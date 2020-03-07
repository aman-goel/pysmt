#
# This file is part of pySMT.
#
#   Copyright 2014 Andrea Micheli and Marco Gario
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
from six.moves import xrange

from pysmt.exceptions import SolverAPINotFound

try:
    import repycudd
except ImportError:
    raise SolverAPINotFound

import pysmt.logics
from pysmt import typing as types
from pysmt.solvers.solver import Solver, Converter, SolverOptions
from pysmt.solvers.eager import EagerModel
from pysmt.walkers import DagWalker
from pysmt.decorators import clear_pending_pop, catch_conversion_error
from pysmt.exceptions import (ConvertExpressionError, PysmtValueError,
                              PysmtTypeError)
from pysmt.oracles import get_logic
from pysmt.solvers.qelim import QuantifierEliminator


class BddOptions(SolverOptions):
    """Options for the BDD Solver.

    * static_ordering: None, list of Symbols
      Enable static ordering with the given list of symbols

    * dynamic_reordering: True, False
      Enable dynamic reordering

    * reordering_algorithm: BddOptions.CUDD_ALL_REORDERING_ALGORITHMS
      Specify which reordering algorithm to use when dynamic_reording
      is enabled.

    """
    ## CUDD Reordering algorithms
    CUDD_ALL_REORDERING_ALGORITHMS = range(1,23)

    CUDD_REORDER_SAME, \
    CUDD_REORDER_NONE, \
    CUDD_REORDER_RANDOM, \
    CUDD_REORDER_RANDOM_PIVOT, \
    CUDD_REORDER_SIFT, \
    CUDD_REORDER_SIFT_CONVERGE, \
    CUDD_REORDER_SYMM_SIFT, \
    CUDD_REORDER_SYMM_SIFT_CONV, \
    CUDD_REORDER_WINDOW2, \
    CUDD_REORDER_WINDOW3, \
    CUDD_REORDER_WINDOW4, \
    CUDD_REORDER_WINDOW2_CONV, \
    CUDD_REORDER_WINDOW3_CONV, \
    CUDD_REORDER_WINDOW4_CONV, \
    CUDD_REORDER_GROUP_SIFT, \
    CUDD_REORDER_GROUP_SIFT_CONV, \
    CUDD_REORDER_ANNEALING, \
    CUDD_REORDER_GENETIC, \
    CUDD_REORDER_LINEAR, \
    CUDD_REORDER_LINEAR_CONVERGE, \
    CUDD_REORDER_LAZY_SIFT, \
    CUDD_REORDER_EXACT = CUDD_ALL_REORDERING_ALGORITHMS

    def __init__(self, **base_options):
        SolverOptions.__init__(self, **base_options)

        if self.random_seed is not None:
            raise PysmtValueError("'random_seed' option not supported.")
        if self.unsat_cores_mode is not None:
            raise PysmtValueError("'unsat_cores_mode' option not supported.")

        for k,v in self.solver_options.items():
            if k == "static_ordering":
                if v is not None:
                    try:
                        valid = all(x.is_symbol(types.BOOL) for x in v)
                    except:
                        valid = False
                    if not valid:
                        raise PysmtValueError("The BDD static ordering must be" \
                                              " a list of Boolean variables")
            elif k == "dynamic_reordering":
                if v not in (True, False):
                    raise PysmtValueError("Invalid value %s for '%s'" % \
                                          (str(k),str(v)))
            elif k == "reordering_algorithm":
                if v not in BddOptions.CUDD_ALL_REORDERING_ALGORITHMS:
                    raise PysmtValueError("Invalid value %s for '%s'" % \
                                          (str(k),str(v)))
            else:
                raise PysmtValueError("Unrecognized option '%s'." % k)
            # Store option
            setattr(self, k, v)

        # Set Defaults
        if not hasattr(self, "dynamic_reordering"):
            self.dynamic_reordering = False
        if not hasattr(self, "reordering_algorithm"):
            if not self.dynamic_reordering:
                self.reordering_algorithm = None
            else:
                self.reordering_algorithm = BddOptions.CUDD_REORDER_SIFT
        if not hasattr(self, "static_ordering"):
            self.static_ordering = None

        # Consistency check
        if not self.dynamic_reordering and self.reordering_algorithm is not None:
            raise PysmtValueError("reordering_algorithm requires "
                                  "dynamic_reordering.")

    def __call__(self, solver):
        # Impose initial ordering
        if self.static_ordering is not None:
            for var in self.static_ordering:
                solver.declare_variable(var)
        if self.dynamic_reordering:
            solver.ddmanager.AutodynEnable(self.reordering_algorithm)
        else:
            solver.ddmanager.AutodynDisable()

# EOC BddOptions


class BddSolver(Solver):
    """CUDD BDD Solver"""
    LOGICS = [ pysmt.logics.QF_BOOL, pysmt.logics.BOOL ]
    OptionsClass = BddOptions

    def __init__(self, environment, logic, **options):
        Solver.__init__(self,
                        environment=environment,
                        logic=logic,
                        **options)

        self.mgr = environment.formula_manager
        self.ddmanager = repycudd.DdManager()
        self.converter = BddConverter(environment=self.environment,
                                      ddmanager=self.ddmanager)
        self.options(self)

        # This stack keeps a pair (Expr, Bdd), with the semantics that
        # the bdd at the i-th element of the list contains the bdd of
        # the conjunction of all previous expressions.
        # The construction of the Bdd is done during solve()
        self.assertions_stack = None
        self.reset_assertions() # Initialize the stack

        self.backtrack = []
        self.latest_model = None

    @clear_pending_pop
    def reset_assertions(self):
        true_formula = self.mgr.Bool(True)
        self.assertions_stack = [(true_formula,
                                  self.converter.convert(true_formula))]

    @clear_pending_pop
    def declare_variable(self, var):
        self.converter.declare_variable(var)

    @clear_pending_pop
    def add_assertion(self, formula, named=None):
        self.assertions_stack.append((formula, None))

    @clear_pending_pop
    def solve(self, assumptions=None):
        if assumptions is not None:
            self.push()
            self.add_assertion(self.mgr.And(assumptions))
            self.pending_pop = True

        for (i, (expr, bdd)) in enumerate(self.assertions_stack):
            if bdd is None:
                bdd_expr = self.converter.convert(expr)
                _, previous_bdd = self.assertions_stack[i-1]
                new_bdd = self.ddmanager.And(previous_bdd, bdd_expr)
                self.assertions_stack[i] = (expr, new_bdd)

        _, current_state = self.assertions_stack[-1]
        res = (current_state != self.ddmanager.Zero())
        # Invalidate cached model
        self.latest_model = None
        return res
    
    def bdd(self):
        _, current_state = self.assertions_stack[-1]
        return current_state

    def get_value(self, item):
        if self.latest_model is None:
            self.get_model()
        return self.latest_model.get_value(item)

    def get_model(self):
        # TODO: We could provide a more sophisticated Model class,
        # that would contain the current Bdd and a copy of the
        # DdManager. This would make it possible to apply other
        # operations on the model (e.g., enumeration) in a simple way.
        if self.latest_model is None:
            _, current_state = self.assertions_stack[-1]
            assert current_state is not None, "solve() should be called before get_model()"
            # Build ddArray of variables
            var_array = self.converter.get_all_vars_array()
            minterm_set = self.ddmanager.PickOneMinterm(current_state,
                                                        var_array,
                                                        len(var_array))
            minterm = next(repycudd.ForeachCubeIterator(self.ddmanager,
                                                        minterm_set))
            assignment = {}
            print("Model:")
            for i, node in enumerate(var_array):
                value = self.mgr.Bool(minterm[i] == 1)
                var = self.converter.idx2var[node.NodeReadIndex()]
                atom = self.converter.var2atom[var]
                assignment[atom] = value
                print("%s := %s = %s" % (atom, var, value))

            self.latest_model = EagerModel(assignment=assignment,
                                           environment=self.environment)
        return self.latest_model

    @clear_pending_pop
    def push(self, levels=1):
        for _ in xrange(levels):
            self.backtrack.append(len(self.assertions_stack))

    @clear_pending_pop
    def pop(self, levels=1):
        for _ in xrange(levels):
            l = self.backtrack.pop()
            self.assertions_stack = self.assertions_stack[:l]

    def _exit(self):
        del self.ddmanager


class BddConverter(Converter, DagWalker):

    def __init__(self, environment, ddmanager):
        DagWalker.__init__(self)

        self.environment = environment
        self.fmgr = self.environment.formula_manager
        self.ddmanager = ddmanager
        # Note: Nodes in repycudd are not shared, but they overload all
        # methods to perform comparison. This means that for two
        # wrappers for the variable x, we have that id(x) != id(x1)
        # but x == x1.  Nevertheless, we need to store the ids, since
        # nodes can be moved during operations by the
        # ddManager. Therefore, when using nodes in a map, we should
        # use the ids.
        self.idx2var = {}
        self.var2node = {}
        self.back_memoization = {}
        self.atom2var = {}
        self.var2atom = {}
        self.atoms = set()
        self._bddEnumConsts = {}
        self._bddEnumVars = {}
        self._bddVarEqEnum = {}
        self.numvars = 0

    @catch_conversion_error
    def convert(self, formula):
        """Convert a PySMT formula into a BDD."""
        return self.walk(formula)

    def back(self, bdd_expr):
        return self._walk_back(bdd_expr, self.fmgr).simplify()

    def get_all_vars_array(self):
        # NOTE: This way of building the var_array does not look
        #       robust.  There might be an issue if variables are
        #       added and the order of enumeration of the dictionary
        #       changes and we rely on this order outside of this class.
        var_array = repycudd.DdArray(self.ddmanager, len(self.idx2var))
        for i, node_idx in enumerate(self.idx2var):
            var_array[i] = self.ddmanager[node_idx]
        return var_array

    def cube_from_var_list(self, var_list):
        indices = repycudd.IntArray(len(var_list))
        for i, v in enumerate(var_list):
            indices[i] = self.var2node[v].NodeReadIndex()
        cube = self.ddmanager.IndicesToCube(indices, len(var_list))
        return cube

    def declare_variable(self, var):
        if not var.is_symbol(type_=types.BOOL):
            raise PysmtTypeError("Trying to declare as a variable something "
                                 "that is not a symbol: %s" % var)
        if var not in self.var2node:
            node = self.ddmanager.NewVar()
            self.numvars += 1
            self.idx2var[node.NodeReadIndex()] = var
            self.var2node[var] = node
#             print("adding var: %s <-> %s" % (var, self.var2node[var]))

    def get_eq_var(self, a):
        if a in self.atom2var:
            return self.atom2var[a]
        if not a.is_equals():
            raise PysmtTypeError("Trying to declare an equality from: %s" % a)
        self.atoms.add(a)
        name = "_eq%d" % len(self.atoms)
        avar = self.fmgr.Symbol(name)
        self.declare_variable(avar)
        self.atom2var[a] = avar
        self.var2atom[avar] = a
#         print("adding vareq: %s := %s <-> %s" % (a, avar, self.var2node[avar]))
        return avar
    
    def get_eq_const(self, lhs, rhs):
        if lhs not in self._bddVarEqEnum:
            self._bddVarEqEnum[lhs] = {}
        if rhs not in self._bddVarEqEnum[lhs]:
            atom = self.fmgr.EqualsOrIff(lhs, rhs)
            avar = self.get_eq_var(atom)
            self._bddVarEqEnum[lhs][rhs] = avar
#             print("adding consteq: %s = %s := %s <-> %s" % (lhs, rhs, avar, self.var2node[avar]))
        avar = self._bddVarEqEnum[lhs][rhs]
        return self.var2node[avar]

    def get_neq_const(self, lhs, rhs):
        eq = self.get_eq_const(lhs, rhs)
        return self.ddmanager.Not(eq)

    def get_function_node(self, func):
        if func in self.atom2var:
            avar = self.atom2var[func]
            return self.var2node[avar]
#         if not func.is_symbol():
#             raise PysmtTypeError("Trying to declare a function from: %s" % func)
        self.atoms.add(func)
        name = "_func%d" % len(self.atoms)
        avar = self.fmgr.Symbol(name)
        self.declare_variable(avar)
        self.atom2var[func] = avar
        self.var2atom[avar] = func
#         print("adding function: %s := %s <-> %s" % (func, avar, self.var2node[avar]))
        return self.var2node[avar]

    def walk_and(self, formula, args, **kwargs):
        res = args[0]
        for a in args[1:]:
            res = self.ddmanager.And(a, res)
        return res

    def walk_or(self, formula, args, **kwargs):
        res = args[0]
        for a in args[1:]:
            res = self.ddmanager.Or(res,a)
        return res

    def walk_not(self, formula, args, **kwargs):
        return self.ddmanager.Not(args[0])

    def walk_implies(self, formula, args, **kwargs):
        lhs, rhs = args
        return self.ddmanager.Or(lhs.Not(), rhs)

    def walk_iff(self, formula, args, **kwargs):
        lhs, rhs = args
        pos = self.ddmanager.And(lhs, rhs)
        neg = self.ddmanager.And(lhs.Not(), rhs.Not())
        return self.ddmanager.Or(pos, neg)

    def walk_forall(self, formula, args, **kwargs):
        f = args[0]
        cube = self.cube_from_var_list(formula.quantifier_vars())
        res = self.ddmanager.UnivAbstract(f, cube)
        return res

    def walk_exists(self, formula, args, **kwargs):
        f = args[0]
        cube = self.cube_from_var_list(formula.quantifier_vars())
        res = self.ddmanager.ExistAbstract(f, cube)
        return res

    def walk_bool_constant(self, formula, **kwargs):
        if formula.is_true():
            return self.ddmanager.One()
        else:
            return self.ddmanager.Zero()

    def walk_enum_constant(self, formula, **kwargs):
        if formula not in self._bddEnumConsts:
            self._bddEnumConsts[formula] = self.ddmanager.One()
        return self._bddEnumConsts[formula]

    def walk_equals(self, formula, args, **kwargs):
        lhs = formula.arg(0)
        rhs = formula.arg(1)
        if rhs.is_symbol() or rhs.is_function_application():
            lhs, rhs = rhs, lhs
        if lhs.is_symbol() or lhs.is_function_application():
            rt = lhs.symbol_type()
            if lhs.is_function_application():
                rt = lhs.return_type
            if rt.is_enum_type():
                rhs = formula.arg(1)
                dom = [self.fmgr.Enum(d, rt) for d in rt.domain]
                res = self.ddmanager.Zero()
                for d in dom:
                    lnode = self.get_eq_const(lhs, d)
                    rnode = self.get_eq_const(rhs, d)
                    node = self.ddmanager.And(lnode, rnode)
                    res = self.ddmanager.Or(res, node)
                for a in args:
                    res = self.ddmanager.And(a, res)
                return res
            elif rt == types.BOOL:
                return self.walk_iff(formula, args, **kwargs)
        raise PysmtTypeError("Trying to declare equality %s" % formula)
        
    def walk_function(self, formula, args, **kwargs):
        f = formula.function_name()
        ft = f.symbol_type()
        if ft.return_type == types.BOOL:
            res = args[0]
            for a in args[1:]:
                res = self.ddmanager.And(a, res)
            node = self.get_function_node(formula)
            res = self.ddmanager.And(res, node)
            return res
        raise PysmtTypeError("Trying to declare function %s of return type %s" % (formula, ft.return_type))
    
    def walk_symbol(self, formula, **kwargs):
        ft = formula.symbol_type()
        if ft.is_enum_type():
            if formula not in self._bddEnumVars:
                res = self.ddmanager.Zero()
                dom = [self.fmgr.Enum(d, ft) for d in ft.domain]
                for d in dom:
                    node = self.get_eq_const(formula, d)
                    res = self.ddmanager.Or(res, node)
                for i in range(len(dom)-1):
                    lhs = self.get_neq_const(formula, dom[i])
                    for j in range(i+1, len(dom)):
                        rhs = self.get_neq_const(formula, dom[j])
                        node = self.ddmanager.Or(lhs, rhs)
                        res = self.ddmanager.And(node, res)
                self._bddEnumVars[formula] = res
            return self._bddEnumVars[formula]
        elif not formula.is_symbol(types.BOOL):
            raise ConvertExpressionError("Cannot handle given formula %s of type %s" % (str(formula), formula.get_type()))
        if formula not in self.var2node:
            self.declare_variable(formula)
        res = self.var2node[formula]
        return res

    def _walk_back(self, bdd, mgr):
        stack = [bdd]
        while len(stack) > 0:
            current = stack.pop()
            # If the node haven't been explored yet, it is not in back_memoization,
            # If back_memoization contains None, we are exploring the children
            # Otherwise, it contains the pySMT expression corresponding to the node
            if current not in self.back_memoization:
                self.back_memoization[current] = None
                stack.append(current)
                if current != self.ddmanager.Zero() and \
                   current != self.ddmanager.One():
                    t = current.T()
                    e = current.E()
                    stack.append(t)
                    stack.append(e)
            elif self.back_memoization[current] is None:
                if current == self.ddmanager.Zero():
                    res = mgr.FALSE()
                elif current == self.ddmanager.One():
                    res = mgr.TRUE()
                else:
                    var = self.idx2var[current.NodeReadIndex()]
                    t = current.T()
                    e = current.E()
                    expr_t = self.back_memoization[t]
                    expr_e = self.back_memoization[e]

                    if current.IsComplement():
                        # (v /\ !t) \/ (!v /\ !e)
                        #     P            N
                        p = mgr.And(var, mgr.Not(expr_t))
                        n = mgr.And(mgr.Not(var), mgr.Not(expr_e))
                        res = mgr.Or(p, n)
                    else:
                        # (v /\ t) \/ (!v /\ e)
                        #    P             N
                        p = mgr.And(var, expr_t)
                        n = mgr.And(mgr.Not(var), expr_e)
                        res = mgr.Or(p, n)

                self.back_memoization[current] = res
            else:
                # we already visited the node, nothing else to do
                pass
        return self.back_memoization[bdd]

# EOC BddConverter


class BddQuantifierEliminator(QuantifierEliminator):

    LOGICS = [pysmt.logics.BOOL]

    def __init__(self, environment, logic=None):
        QuantifierEliminator.__init__(self)
        self.environment = environment
        self.logic = logic
        self.ddmanager = repycudd.DdManager()
        self.converter = BddConverter(environment=environment,
                                      ddmanager=self.ddmanager)

    def eliminate_quantifiers(self, formula):
        logic = get_logic(formula, self.environment)
        if not logic <= pysmt.logics.BOOL:
            raise NotImplementedError("BDD-based quantifier elimination only "\
                                      "supports pure-boolean formulae."\
                                      "(detected logic is: %s)" % str(logic))

        bdd = self.converter.convert(formula)
        pysmt_res = self.converter.back(bdd)
        return pysmt_res

    def _exit(self):
        del self.ddmanager
