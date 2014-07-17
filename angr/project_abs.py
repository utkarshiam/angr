#!/usr/bin/env python

import simuvex
import logging
import angr
# pylint: disable=W0201
# pylint: disable=W0703

l = logging.getLogger("angr.project")

# This is a factory method to choose which class to instantiate.
def Project(filename, arch=None, binary_base_addr=None,
                 load_libs=None, resolve_imports=None,
                 use_sim_procedures=None, exclude_sim_procedures=(),
                 exclude_sim_procedure=lambda x: False,
                 default_analysis_mode=None, allow_pybfd=True,
            allow_r2=True, use_cle=False):

    if use_cle == True:
        return angr.Project_cle(filename, use_sim_procedures, arch,
                                           exclude_sim_procedures,
                                           default_analysis_mode)
    else:
        return angr.Project_ida(filename, arch, binary_base_addr, load_libs,
                           resolve_imports, use_sim_procedures,
                           exclude_sim_procedures, exclude_sim_procedure,
                           default_analysis_mode, allow_pybfd, allow_r2)

class AbsProject(object):
    """ This class contains all the stuff in common between Project_cle and
    project_ida """

    def initial_exit(self, mode=None, options=None):
        """Creates a SimExit to the entry point."""
        return self.exit_to(self.entry, mode=mode, options=options)

    def initial_state(self, options=None, mode=None):
        """Creates an initial state, with stack and everything."""
        if mode is None and options is None:
            mode = self.default_analysis_mode
        s = simuvex.SimState(memory_backer=self.mem, arch=self.arch, mode=mode,
                             options=options).copy()

        # Initialize the stack pointer
        if s.arch.name == "AMD64":
            s.store_reg(176, 1, 8)
            s.store_reg(s.arch.sp_offset, 0xfffffffffff0000, 8)
        elif s.arch.name == "X86":
            s.store_reg(s.arch.sp_offset, 0x7fff0000, 4)
        elif s.arch.name == "ARM":
            s.store_reg(s.arch.sp_offset, 0xffff0000, 4)

            # the freaking THUMB state
            s.store_reg(0x188, 0x00000000, 4)
        elif s.arch.name == "PPC32":
            # TODO: Is this correct?
            s.store_reg(s.arch.sp_offset, 0xffff0000, 4)
        elif s.arch.name == "MIPS32":
            # TODO: Is this correct?
            s.store_reg(s.arch.sp_offset, 0xffff0000, 4)
        else:
            raise Exception("Architecture %s is not supported." % s.arch.name)
        return s

    def exit_to(self, addr, state=None, mode=None, options=None, jumpkind=None):
        """Creates a SimExit to the specified address."""
        if state is None:
            state = self.initial_state(mode=mode, options=options)
        return simuvex.SimExit(addr=addr, state=state, jumpkind=jumpkind)

    def block(self, addr, max_size=None, num_inst=None, traceflags=0):
        """
        Returns a pyvex block starting at address addr

        Optional params:

        @param max_size: the maximum size of the block, in bytes
        @param num_inst: the maximum number of instructions
        @param traceflags: traceflags to be passed to VEX. Default: 0
        """
        thumb = False
        if self.arch.name == "ARM":
            if self.binary_by_addr(addr) is None:
                raise AngrMemoryError("No IDA to check thumb mode at 0x%x." % addr)

            if self.binary_by_addr(addr).ida.idc.GetReg(addr, "T") == 1:
                thumb = True

        return self.vexer.block(addr, max_size=max_size, num_inst=num_inst,
                                traceflags=traceflags, thumb=thumb)

    def sim_block(self, where, max_size=None, num_inst=None,
                  stmt_whitelist=None, last_stmt=None):
        """
        Returns a simuvex block starting at SimExit 'where'

        Optional params:

        @param where: the exit to start the analysis at
        @param max_size: the maximum size of the block, in bytes
        @param num_inst: the maximum number of instructions
        @param state: the initial state. Fully unconstrained if None

        """
        irsb = self.block(where.concretize(), max_size, num_inst)
        return simuvex.SimIRSB(where.state, irsb, addr=where.concretize(),
                               whitelist=stmt_whitelist, last_stmt=last_stmt)

    def sim_run(self, where, max_size=400, num_inst=None, stmt_whitelist=None,
                last_stmt=None):
        """
        Returns a simuvex SimRun object (supporting refs() and
        exits()), automatically choosing whether to create a SimIRSB or
        a SimProcedure.

        Parameters:
        @param where : the exit to analyze
        @param max_size : the maximum size of the block, in bytes
        @param num_inst : the maximum number of instructions
        @param state : the initial state. Fully unconstrained if None
        """

        if where.is_error:
            raise AngrExitError("Provided exit of jumpkind %s is in an error "
                                "state.", where.jumpkind)

        addr = where.concretize()
        state = where.state

        if addr % state.arch.instruction_alignment != 0:
            raise AngrExitError("Address 0x%x does not align to alignment %d for"
                                " architecture %s." % (addr,
                                state.arch.instruction_alignment,
                                state.arch.name))

        if where.is_syscall:
            l.debug("Invoking system call handler (originally at 0x%x)", addr)
            return simuvex.SimProcedures['syscalls']['handler'](state, addr=addr)
        if self.is_sim_procedure(addr):
            sim_proc_class, kwargs = self.sim_procedures[addr]
            l.debug("Creating SimProcedure %s (originally at 0x%x)",
                    sim_proc_class.__name__, addr)
            return sim_proc_class(state, addr=addr, **kwargs)
        else:
            l.debug("Creating SimIRSB at 0x%x", addr)
            return self.sim_block(where, max_size=max_size, num_inst=num_inst,
                                  stmt_whitelist=stmt_whitelist,
                                  last_stmt=last_stmt)

    def add_custom_sim_procedure(self, address, sim_proc, kwargs):
        '''
        Link a SimProcedure class to a specified address.
        '''
        if address in self.sim_procedures:
            l.warning("Address 0x%08x is already in SimProcedure dict.", address)
            return
        if kwargs is None: kwargs = {}
        self.sim_procedures[address] = (sim_proc, kwargs)

    def is_sim_procedure(self, hashed_addr):
        return hashed_addr in self.sim_procedures

    def get_pseudo_addr_for_sim_procedure(self, s_proc):
        for addr, tpl in self.sim_procedures.items():
            simproc_class, _ = tpl
            if isinstance(s_proc, simproc_class):
                return addr
        return None

    def construct_cfg(self, avoid_runs=[]):
        """ Constructs a control flow graph """
        c = CFG()
        c.construct(self.main_binary, self, avoid_runs=avoid_runs)
        return c

#from .binary import Binary
#from .memory_dict import MemoryDict
from .errors import AngrMemoryError, AngrExitError
#from .vexer import VEXer
from .cfg import CFG
#from .project_cle import Project_cle
#from .project_ida import Project_ida

