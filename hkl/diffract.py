"""
diffract
--------

Common Support for diffractometers

.. autosummary::

    ~Constraint
    ~Diffractometer

"""


import collections
from ophyd import Component as Cpt
from ophyd import PositionerBase
from ophyd import PseudoPositioner
from ophyd import Signal
from ophyd.pseudopos import pseudo_position_argument
from ophyd.pseudopos import real_position_argument
from ophyd.signal import AttributeSignal, ArrayAttributeSignal
import logging
import pint
import pyRestTable

from . import calc


logger = logging.getLogger(__name__)


Constraint = collections.namedtuple("Constraint", ("low_limit", "high_limit", "value", "fit"))


class Diffractometer(PseudoPositioner):
    """Diffractometer pseudopositioner

    .. autosummary::

        ~calc
        ~engine
        ~forward
        ~inverse
        ~forward_solutions_table
        ~apply_constraints
        ~reset_constraints
        ~show_constraints
        ~undo_last_constraints
        ~pa
        ~wh
        ~_energy_changed
        ~_update_calc_energy


    This has a corresponding calculation engine from **hklpy** that does
    forward and inverse calculations.

    If instantiating a specific diffractometer class such as `E4C`, `E6C`,
    neither the `calc_inst` or the `calc_kw` parameters are required.

    However, there is the option to either pass in a calculation
    instance (with `calc_inst`) or keywords for the default calculation
    class (using `calc_kw`) to instantiate a new one.

    Parameters
    ----------
    prefix : str
        PV prefix for all components
    calc_kw : dict, optional
        Initializer arguments for the calc class
    decision_fcn : callable, optional
        The decision function to use when multiple solutions exist for a given
        forward calculation. Defaults to arbitrarily picking the first
        solution.
    read_attrs : list, optional
        Read attributes default to all pseudo and real positioners
    configuration_attrs : list, optional
        Defaults to the UB matrix and energy
    parent : Device, optional
        Parent device
    name : str, optional
        Device name

    Attributes
    ----------
    calc_class : sub-class of CalcRecip
        Reciprocal calculation class used with this diffractometer.
        If None (as in `hkl.diffract.Diffractometer`, `calc_inst` must be
        specified upon initialization.

    See Also
    --------
    :class:`hkl.diffract.E4CH`
    :class:`hkl.diffract.E4CV`
    :class:`hkl.diffract.E6C`
    :class:`hkl.diffract.K4CV`
    :class:`hkl.diffract.K6C`
    :class:`hkl.diffract.Med2p3`
    :class:`hkl.diffract.Petra3_p09_eh2`
    :class:`hkl.diffract.SoleilMars`
    :class:`hkl.diffract.SoleilSiriusKappa`
    :class:`hkl.diffract.SoleilSiriusTurret`
    :class:`hkl.diffract.SoleilSixs`
    :class:`hkl.diffract.SoleilSixsMed1p2`
    :class:`hkl.diffract.SoleilSixsMed2p2`
    :class:`hkl.diffract.TwoC`
    :class:`hkl.diffract.Zaxis`
    """

    calc_class = None

    # see: Documentation has examples to use an EPICS PV for energy.
    energy = Cpt(Signal, value=8.0, doc="Energy (in keV)")
    energy_units = Cpt(Signal, value="keV")
    energy_offset = Cpt(Signal, value=0)
    energy_update_calc_flag = Cpt(Signal, value=True)

    geometry_name = Cpt(
        # fmt: off
        AttributeSignal,
        attr="calc.geometry_name",
        doc="Diffractometer Geometry name",
        write_access=False,
        # fmt: on
    )
    class_name = Cpt(
        # fmt: off
        AttributeSignal,
        attr="__class__.__name__",
        doc="Diffractometer class name",
        write_access=False,
        # fmt: on
    )

    sample_name = Cpt(AttributeSignal, attr="calc.sample_name", doc="Sample name")
    lattice = Cpt(
        # fmt: off
        ArrayAttributeSignal,
        attr="calc.sample.lattice",
        doc="Sample lattice",
        # fmt: on
    )
    lattice_reciprocal = Cpt(
        # fmt: off
        AttributeSignal,
        attr="calc.sample.reciprocal",
        doc="Reciprocal lattice",
        # fmt: on
    )

    U = Cpt(AttributeSignal, attr="calc.sample.U", doc="U matrix")
    UB = Cpt(AttributeSignal, attr="calc.sample.UB", doc="UB matrix")
    # fmt: off
    reflections = Cpt(
        ArrayAttributeSignal,
        attr="calc.sample.reflections",
        doc="Reflections",
    )
    reflections_details = Cpt(
        AttributeSignal,
        attr="calc.sample.reflections_details",
        doc="Details of reflections",
    )
    ux = Cpt(
        AttributeSignal,
        attr="calc.sample.ux.value",
        doc="ux portion of the U matrix",
    )
    uy = Cpt(
        AttributeSignal,
        attr="calc.sample.uy.value",
        doc="uy portion of the U matrix",
    )
    uz = Cpt(
        AttributeSignal,
        attr="calc.sample.uz.value",
        doc="uz portion of the U matrix",
    )
    # fmt: on

    def __init__(
        self,
        prefix,
        calc_kw=None,
        decision_fcn=None,
        calc_inst=None,
        *,
        configuration_attrs=None,
        read_attrs=None,
        **kwargs,
    ):
        if calc_inst is not None:
            if not isinstance(calc_inst, self.calc_class):
                raise ValueError("Calculation instance must be derived  from the class {self.calc_class}")
            self._calc = calc_inst

        else:
            if calc_kw is None:
                calc_kw = {}

            self._calc = self.calc_class(lock_engine=True, **calc_kw)

        if not self.calc.engine_locked:
            # Reason for this is that the engine determines the pseudomotor
            # names, so if the engine is switched from underneath, the
            # pseudomotor will no longer function properly
            raise ValueError("Calculation engine must be locked  (CalcDiff.lock_engine set)")

        if configuration_attrs is None:
            configuration_attrs = """
                UB energy reflections_details geometry_name class_name
            """.split()

        if decision_fcn is None:
            # the default decision function is to just grab solution #1:
            decision_fcn = calc.default_decision_function

        self._decision_fcn = decision_fcn

        super().__init__(
            # fmt: off
            prefix,
            read_attrs=read_attrs,
            configuration_attrs=configuration_attrs,
            **kwargs,
            # fmt: on
        )

        self._constraints_stack = []

        if read_attrs is None:
            # if unspecified, set the read attrs to the pseudo/real motor
            # positions once known
            self.read_attrs = list(self.PseudoPosition._fields) + list(self.RealPosition._fields)

        self.energy.subscribe(self._energy_changed, event_type=Signal.SUB_VALUE)

        self.energy_offset.subscribe(self._energy_offset_changed, event_type=Signal.SUB_VALUE)

        self.energy_units.subscribe(self._energy_units_changed, event_type=Signal.SUB_VALUE)

    @property
    def _calc_energy_update_permitted(self):
        """return boolean `True` if permitted"""
        acceptable_values = (1, "Yes", "locked", "OK", True, "On")
        return self.energy_update_calc_flag.get() in acceptable_values

    def _energy_changed(self, value=None, **kwargs):
        """
        Callback indicating that the energy signal was updated

        .. note::
            The `energy` signal is subscribed to this method
            in the :meth:`Diffractometer.__init__()` method.
        """
        if not self.connected:
            logger.warning(
                # fmt: off
                "%s not fully connected, %s.calc.energy not updated",
                self.name,
                self.name,
                # fmt: on
            )
            return

        if self._calc_energy_update_permitted:
            self._update_calc_energy(value)

    def _energy_offset_changed(self, value=None, **kwargs):
        """
        Callback indicating that the energy offset signal was updated.

        .. note::
            The ``energy_offset`` signal is subscribed to this method
            in the :meth:`Diffractometer.__init__()` method.
        """
        if not self.connected:
            logger.warning(
                # fmt: off
                ("%s not fully connected, '%s.calc.energy_offset' not updated"),
                self.name,
                self.name,
                # fmt: on
            )
            return

        # TODO: is there a loop back through _update_calc_energy?
        units = self.energy_units.get()
        try:
            energy = pint.Quantity(self.calc.energy, "keV").to(units)
        except Exception as exc:
            raise NotImplementedError(f"units = {units}: {exc}")
        self.energy.put(energy.magnitude - value)

    def _energy_units_changed(self, value=None, **kwargs):
        """
        Callback indicating that the energy units signal was updated.

        .. note::
            The ``energy_units`` signal is subscribed to this method
            in the :meth:`Diffractometer.__init__()` method.
        """
        if not self.connected:
            logger.warning(
                # fmt: off
                ("%s not fully connected, '%s.calc.energy_units' not updated"),
                self.name,
                self.name,
                # fmt: on
            )
            return

        # TODO: is there a loop back through _update_calc_energy?
        energy = pint.Quantity(self.calc.energy, "keV").to(value)
        self.energy.put(energy.magnitude - self.energy_offset.get())

    def _update_calc_energy(self, value=None, **kwargs):
        """
        writes ``self.calc.energy`` from ``value`` or ``self.energy``.
        """
        if not self.connected:
            logger.warning(
                # fmt: off
                "%s not fully connected, %s.calc.energy not updated",
                self.name,
                self.name,
                # fmt: on
            )
            return

        # use either supplied value or get from signal
        value = float(value or self.energy.get())

        # energy_offset has same units as energy
        value += self.energy_offset.get()

        # comment these lines to skip unit conversion
        units = self.energy_units.get()
        if units != "keV":
            keV = pint.Quantity(value, units).to("keV")
            value = keV.magnitude

        logger.debug("setting %s.calc.energy = %f (keV)", self.name, value)
        self.calc.energy = value
        self._update_position()

    @property
    def calc(self):
        """The calculation instance"""
        return self._calc

    @property
    def engine(self):
        """The calculation engine associated with the diffractometer"""
        return self.calc.engine

    # TODO so these calculations change the internal state of the hkl
    # calculation class, which is probably not a good thing
    # -- it becomes a problem when someone uses these functions
    # outside of move()

    @pseudo_position_argument
    def forward(self, pseudo):
        solutions = self.calc.forward_iter(start=self.position, end=pseudo, max_iters=100)
        logger.debug("pseudo to real: %s", solutions)
        return self._decision_fcn(pseudo, solutions)

    @real_position_argument
    def inverse(self, real):
        self.calc.physical_positions = real
        return self.PseudoPosition(*self.calc.pseudo_positions)

    def check_value(self, pos):
        """
        Raise exception if pos is not within limits.

        In a scan, a subset of the pseudo axes may be directed,
        which are given in a dict from a set message from the
        bluesky RunEngine.

        It is not permitted to scan both pseudo and real positioners.
        """
        if isinstance(pos, dict):
            # Redefine and fill in any missing values.

            for axis, target in pos.items():
                if hasattr(self, axis):
                    p = getattr(self, axis)
                    if p in self.real_positioners:
                        p.check_value(target)
                else:
                    raise KeyError(f"{axis} not in {self.name}")

            pos = [pos.get(p.attr_name, p.position) for p in self.pseudo_positioners]
        super().check_value(pos)

    def apply_constraints(self, constraints):
        """
        Constrain the diffractometer's motions.

        This action will first the current constraints onto
        a stack, enabling both *undo* and *reset* features.
        """
        self._push_current_constraints()
        self._set_constraints(constraints)

    def reset_constraints(self):
        """Set constraints back to initial settings."""
        if len(self._constraints_stack) > 0:
            self._set_constraints(self._constraints_stack[0])
            self._constraints_stack = []

    def show_constraints(self, fmt="simple", printing=True):
        """Print the current constraints in a table."""
        tbl = pyRestTable.Table()
        tbl.labels = "axis low_limit high_limit value fit".split()
        for m in self.real_positioners._fields:
            # fmt: off
            tbl.addRow(
                (
                    m,
                    *self.calc[m].limits,
                    self.calc[m].value,
                    self.calc[m].fit,
                )
            )
            # fmt: on

        if printing:
            print(tbl.reST(fmt=fmt))

        return tbl

    def undo_last_constraints(self):
        """
        Remove the current constraints additions, restore previous.
        """
        if len(self._constraints_stack) > 0:
            self._set_constraints(self._constraints_stack.pop())

    def _push_current_constraints(self):
        """push current constraints onto the stack"""
        constraints = {
            m: Constraint(*self.calc[m].limits, self.calc[m].value, self.calc[m].fit)
            for m in self.real_positioners._fields
            # TODO: any other positioner constraints
        }
        self._constraints_stack.append(constraints)

    def _set_constraints(self, constraints):
        """set diffractometer's constraints"""
        for axis, constraint in constraints.items():
            self.calc[axis].limits = (
                constraint.low_limit,
                constraint.high_limit,
            )
            self.calc[axis].value = constraint.value
            self.calc[axis].fit = constraint.fit

    def forward_solutions_table(self, reflections, full=False):
        """
        Return table of computed solutions for each (hkl) in the supplied reflections list.

        The solutions are calculated using the current UB matrix & constraints
        """
        _table = pyRestTable.Table()
        motors = self.real_positioners._fields
        _table.labels = "(hkl) solution".split() + list(motors)
        for reflection in reflections:
            try:
                solutions = self.calc.forward(reflection)
            except ValueError as exc:
                solutions = exc
            if isinstance(solutions, ValueError):
                row = [reflection, "none"]
                row += ["" for m in motors]
                _table.addRow(row)
            else:
                for i, s in enumerate(solutions):
                    row = [reflection, i]
                    row += [f"{getattr(s, m):.5f}" for m in motors]
                    _table.addRow(row)
                    if not full:
                        break  # only show the first (default) solution
        return _table

    def pa(self, all_samples=False, printing=True):
        """
        Report the diffractometer settings.

        EXAMPLE::

            In [1]: from hkl.geometries import SimulatedK4CV

            In [2]: k4cv = SimulatedK4CV('', name='k4cv')

            In [3]: k4cv.pa()  # FIXME lines are too long to include in source code
            ===================== ====================================================================
            term                  value
            ===================== ====================================================================
            diffractometer        k4cv
            geometry              K4CV
            class                 SimulatedK4CV
            energy (keV)          8.05092
            wavelength (angstrom) 1.54000
            calc engine           hkl
            mode                  bissector
            positions             ====== =======
                                name   value
                                ====== =======
                                komega 0.00000
                                kappa  0.00000
                                kphi   0.00000
                                tth    0.00000
                                ====== =======
            sample: main          ================ ===================================================
                                term             value
                                ================ ===================================================
                                unit cell edges  a=1.54, b=1.54, c=1.54
                                unit cell angles alpha=90.0, beta=90.0, gamma=90.0
                                [U]              [[1. 0. 0.]
                                                    [0. 1. 0.]
                                                    [0. 0. 1.]]
                                [UB]             [[ 4.07999046e+00 -2.49827363e-16 -2.49827363e-16]
                                                    [ 0.00000000e+00  4.07999046e+00 -2.49827363e-16]
                                                    [ 0.00000000e+00  0.00000000e+00  4.07999046e+00]]
                                ================ ===================================================
            ===================== ====================================================================

            Out[3]: <pyRestTable.rest_table.Table at 0x7f5c16503e20>

        """

        def addTable(tbl):
            return str(tbl).strip()

        def Package(**kwargs):
            return ", ".join([f"{k}={v}" for k, v in kwargs.items()])

        table = pyRestTable.Table()
        table.labels = "term value".split()

        table.addRow(("diffractometer", self.name))
        table.addRow(("geometry", self.calc._geometry.name_get()))
        table.addRow(("class", self.__class__.__name__))
        table.addRow(("energy (keV)", f"{self.calc.energy:.5f}"))
        table.addRow(("wavelength (angstrom)", f"{self.calc.wavelength:.5f}"))
        table.addRow(("calc engine", self.calc.engine.name))
        table.addRow(("mode", self.calc.engine.mode))

        pt = pyRestTable.Table()
        pt.labels = "name value".split()
        if self.calc._axis_name_to_original:
            pt.addLabel("original name")
        for item in self.real_positioners:
            row = [item.attr_name, f"{item.position:.5f}"]
            k = self.calc._axis_name_to_original.get(item.attr_name)
            if k is not None:
                row.append(k)
            pt.addRow(row)
        table.addRow(("positions", addTable(pt)))

        t = self.show_constraints(printing=False)
        table.addRow(("constraints", addTable(t)))

        if all_samples:
            samples = self.calc._samples.values()
        else:
            samples = [self.calc._sample]
        for sample in samples:
            t = pyRestTable.Table()
            t.labels = "term value".split()
            nm = sample.name
            if all_samples and sample == self.calc.sample:
                nm += " (*)"

            # fmt: off
            t.addRow(
                (
                    "unit cell edges",
                    Package(
                        **{
                            k: getattr(sample.lattice, k)
                            for k in "a b c".split()
                        }
                    ),
                )
            )
            t.addRow(
                (
                    "unit cell angles",
                    Package(
                        **{
                            k: getattr(sample.lattice, k)
                            for k in "alpha beta gamma".split()
                        }
                    ),
                )
            )
            # fmt: on

            for i, ref in enumerate(sample._sample.reflections_get()):
                h, k, l = ref.hkl_get()
                pos_arr = ref.geometry_get().axis_values_get(self.calc._units)
                t.addRow((f"ref {i+1} (hkl)", Package(**dict(h=h, k=k, l=l))))
                # fmt: off
                t.addRow(
                    (
                        f"ref {i+1} positioners",
                        Package(
                            **{
                                k: f"{v:.5f}"
                                for k, v in zip(
                                    self.calc.physical_axis_names, pos_arr
                                )
                            }
                        ),
                    )
                )
                # fmt: on

            t.addRow(("[U]", sample.U))
            t.addRow(("[UB]", sample.UB))

            table.addRow((f"sample: {nm}", addTable(t)))

        if printing:
            print(table)

        return table

    def wh(self, printing=True):
        """
        report where is the diffractometer

        EXAMPLE::

            In [1]: from hkl.geometries import SimulatedK4CV

            In [2]: k4cv = SimulatedK4CV('', name='k4cv')

            In [3]: k4cv.wh()
            ===================== ========= =========
            term                  value     axis_type
            ===================== ========= =========
            diffractometer        k4cv
            sample name           main
            energy (keV)          8.05092
            wavelength (angstrom) 1.54000
            calc engine           hkl
            mode                  bissector
            h                     0.0       pseudo
            k                     0.0       pseudo
            l                     0.0       pseudo
            komega                0         real
            kappa                 0         real
            kphi                  0         real
            tth                   0         real
            ===================== ========= =========

            Out[3]: <pyRestTable.rest_table.Table at 0x7f55c4775cd0>

        compare with similar function in SPEC::

            1117.KAPPA> wh
            H K L =  0  0  1.7345
            Alpha = 20  Beta = 20  Azimuth = 90
            Omega = 32.952  Lambda = 1.54
            Two Theta       Theta         Chi         Phi     K_Theta       Kappa       K_Phi
            40.000000   20.000000   90.000000   57.048500   77.044988  134.755995  114.093455

        """
        table = pyRestTable.Table()
        table.labels = "term value axis_type".split()
        table.addRow(("diffractometer", self.name, ""))
        table.addRow(("sample name", self.calc.sample.name, ""))
        table.addRow(("energy (keV)", f"{self.calc.energy:.5f}", ""))
        table.addRow(("wavelength (angstrom)", f"{self.calc.wavelength:.5f}", ""))
        table.addRow(("calc engine", self.calc.engine.name, ""))
        table.addRow(("mode", self.calc.engine.mode, ""))

        pseudo_axes = [v.attr_name for v in self._pseudo]
        real_axes = [v.attr_name for v in self._real]
        for k in self._sig_attrs.keys():
            v = getattr(self, k)
            if not issubclass(v.__class__, PositionerBase):
                continue
            if k in real_axes:
                label = "real"
            elif k in pseudo_axes:
                label = "pseudo"
            else:
                label = "additional"
            table.addRow((k, v.position, label))

        if printing:
            print(table)

        return table
