import logging

from miasm2.ir.symbexec import SymbolicExecutionEngine
from miasm2.expression.expression import ExprMem
from miasm2.expression.expression_helper import possible_values
from miasm2.expression.simplifications import expr_simp
from miasm2.ir.ir import IRBlock, AssignBlock

LOG_CST_PROPAG = logging.getLogger("cst_propag")
CONSOLE_HANDLER = logging.StreamHandler()
CONSOLE_HANDLER.setFormatter(logging.Formatter("%(levelname)-5s: %(message)s"))
LOG_CST_PROPAG.addHandler(CONSOLE_HANDLER)
LOG_CST_PROPAG.setLevel(logging.WARNING)


class SymbExecState(SymbolicExecutionEngine):
    """
    State manager for SymbolicExecution
    """
    def __init__(self, ir_arch, state):
        super(SymbExecState, self).__init__(ir_arch, {})
        self.set_state(state)


def add_state(ir_arch, todo, states, addr, state):
    """
    Add or merge the computed @state for the block at @addr. Update @todo
    @ir_arch: IR instance
    @todo: modified block set
    @states: dictionnary linking a label to its entering state.
    @addr: address of the concidered block
    @state: computed state
    """
    addr = ir_arch.get_label(addr)
    todo.add(addr)
    if addr not in states:
        states[addr] = state
    else:
        states[addr] = states[addr].merge(state)


def is_expr_cst(ir_arch, expr):
    """Return true if @expr is only composed of ExprInt and init_regs
    @ir_arch: IR instance
    @expr: Expression to test"""

    elements = expr.get_r(mem_read=True)
    for element in elements:
        if element.is_mem():
            continue
        if element.is_id() and element in ir_arch.arch.regs.all_regs_ids_init:
            continue
        if element.is_int():
            continue
        return False
    else:
        # Expr is a constant
        return True


class SymbExecStateFix(SymbolicExecutionEngine):
    """
    Emul blocks and replace expressions with their corresponding constant if
    any.

    """
    # Function used to test if an Expression is considered as a constant
    is_expr_cst = lambda _, ir_arch, expr: is_expr_cst(ir_arch, expr)

    def __init__(self, ir_arch, state, cst_propag_link):
        super(SymbExecStateFix, self).__init__(ir_arch, {})
        self.set_state(state)
        self.cst_propag_link = cst_propag_link

    def propag_expr_cst(self, expr):
        """Propagate consttant expressions in @expr
        @expr: Expression to update"""
        elements = expr.get_r(mem_read=True)
        to_propag = {}
        for element in elements:
            # Only ExprId can be safely propagated
            if not element.is_id():
                continue
            value = self.eval_expr(element)
            if self.is_expr_cst(self.ir_arch, value):
                to_propag[element] = value
        return expr_simp(expr.replace_expr(to_propag))

    def emulbloc(self, irb, step=False):
        """
        Symbolic execution of the @irb on the current state
        @irb: IRBlock instance
        @step: display intermediate steps
        """
        assignblks = []
        for index, assignblk in enumerate(irb.irs):
            new_assignblk = {}
            links = {}
            for dst, src in assignblk.iteritems():
                src = self.propag_expr_cst(src)
                if dst.is_mem():
                    ptr = dst.arg
                    ptr = self.propag_expr_cst(ptr)
                    dst = ExprMem(ptr, dst.size)
                new_assignblk[dst] = src

            for arg in assignblk.instr.args:
                new_arg = self.propag_expr_cst(arg)
                links[new_arg] = arg
            self.cst_propag_link[(irb.label, index)] = links

            self.eval_ir(assignblk)
            assignblks.append(AssignBlock(new_assignblk, assignblk.instr))
        self.ir_arch.blocks[irb.label] = IRBlock(irb.label, assignblks)


def compute_cst_propagation_states(ir_arch, init_addr, init_infos):
    """
    Propagate "constant expressions" in a function.
    The attribute "constant expression" is true if the expression is based on
    constants or "init" regs values.

    @ir_arch: IntermediateRepresentation instance
    @init_addr: analysis start address
    @init_infos: dictionnary linking expressions to their values at @init_addr
    """

    done = set()
    state = SymbExecState.StateEngine(init_infos)
    lbl = ir_arch.get_label(init_addr)
    todo = set([lbl])
    states = {lbl: state}

    while todo:
        if not todo:
            break
        lbl = todo.pop()
        state = states[lbl]
        if (lbl, state) in done:
            continue
        done.add((lbl, state))
        symbexec_engine = SymbExecState(ir_arch, state)

        assert lbl in ir_arch.blocks
        addr = symbexec_engine.emul_ir_block(lbl)
        symbexec_engine.del_mem_above_stack(ir_arch.sp)

        for dst in possible_values(addr):
            value = dst.value
            if value.is_mem():
                LOG_CST_PROPAG.warning('Bad destination: %s', value)
                continue
            elif value.is_int():
                value = ir_arch.get_label(value)
            add_state(ir_arch, todo, states, value,
                      symbexec_engine.get_state())

    return states


def propagate_cst_expr(ir_arch, addr, init_infos):
    """
    Propagate "constant expressions" in a @ir_arch.
    The attribute "constant expression" is true if the expression is based on
    constants or "init" regs values.

    @ir_arch: IntermediateRepresentation instance
    @addr: analysis start address
    @init_infos: dictionnary linking expressions to their values at @init_addr

    Returns a mapping between replaced Expression and their new values.
    """
    states = compute_cst_propagation_states(ir_arch, addr, init_infos)
    cst_propag_link = {}
    for lbl, state in states.iteritems():
        symbexec = SymbExecStateFix(ir_arch, state, cst_propag_link)
        symbexec.emulbloc(ir_arch.blocks[lbl])
    return cst_propag_link
