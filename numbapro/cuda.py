import sys
import logging
logger = logging.getLogger(__name__)

from llvm import core as _lc
from llvm import ee as _le
from llvm import passes as _lp
from llvm_cbuilder import shortnames as _llvm_cbuilder_types
from _cuda.sreg import threadIdx, blockIdx, blockDim, gridDim
from _cuda.smem import shared
from _cuda.barrier import syncthreads
from _cuda.macros import grid
from _cuda.transform import function_cache
from _cuda import ptx
from numba.minivect import minitypes
from numba import void
import numba.decorators
import _cuda.default # this creates the default context

cached = {}
def jit(restype=void, argtypes=None, backend='ast', **kws):
    '''JIT python function into a CUDA kernel.

    A CUDA kernel does not return any value.
    To retrieve result, use an array as a parameter.
    By default, all array data will be copied back to the host.
    Scalar parameter is copied to the host and will be not be read back.
    It is not possible to pass scalar parameter by reference.

    Support for double-precision floats depends on your CUDA device.
    '''
    if isinstance(restype, minitypes.FunctionType):
        if argtypes is not None:
            raise TypeError, "Cannot use both calling syntax and argtypes keyword"
        argtypes = restype.args
        restype = restype.return_type
        name = restype.name
    # Called with a string like 'f8(f8)'
    elif isinstance(restype, str) and argtypes is None:
        name, restype, argtypes = numba.decorators._process_sig(restype,
                                                    kws.get('name', None))

    assert argtypes is not None
    assert backend == 'ast', 'Bytecode support has dropped'

    #    restype = int32
    #    if backend=='bytecode':
    #        key = func, tuple(get_strided_arrays(argtypes))
    #        if key in cached:
    #            cnf = cached[key]
    #        else:
    #            # NOTE: This will use bytecode translate path
    #            t = CudaTranslate(func, restype=restype, argtypes=argtypes,
    #                              module=_lc.Module.new("ptx_%s" % str(func)))
    #            t.translate()

    #            cnf = CudaNumbaFunction(func, lfunc=t.lfunc, extra=t)
    #            cached[key] = cnf

    #        return cnf
    #    else:
    return jit2(restype=restype, argtypes=argtypes, **kws)


_device_functions = {}

def _ast_jit(func, argtypes, inline, llvm_module, **kws):
    kws['nopython'] = True          # override nopython option

    if not hasattr(func, '_is_numba_func'):
        func._is_numba_func = True
        func._numba_compile_only = True
    assert func._numba_compile_only
    func._numba_inline = inline
    result = function_cache.compile_function(func, argtypes,
                                             ctypes=True,
                                             llvm_module=llvm_module,
                                             _llvm_ee=None,
                                             **kws)
    signature, lfunc, unused = result
    assert unused is None               # Just to be sure

    # XXX: temp fix for PyIncRef and PyDecRef in lfunc.
    # IS this still necessary?
    # Yes, as of Oct 22 2012
    _temp_fix_to_remove_python_specifics(lfunc)

    return signature, lfunc

def jit2(restype=void, argtypes=None, device=False, inline=False, **kws):
    if restype == None: restype = void
    assert device or restype == void,\
           ("Only device function can have return value %s" % restype)
    if kws.pop('_llvm_ee', None) is not None:
        raise Exception("_llvm_ee should not be defined.")
    assert not inline or device
    def _jit2(func):
        llvm_module = (kws.pop('llvm_module', None)
                       or _lc.Module.new('ptx_%s' % func))
        
        signature, lfunc = _ast_jit(func, argtypes, inline, llvm_module, **kws)
        
        if device:
            assert lfunc.name not in _device_functions, 'Device function name already used'
            _device_functions[lfunc.name] = func, lfunc
            return CudaDeviceFunction(func, signature=signature, lfunc=lfunc)
        else:
            return CudaNumbaFunction(func, signature=signature, lfunc=lfunc)
    return _jit2


def _list_callinstr(lfunc):
    for bb in lfunc.basic_blocks:
        for instr in bb.instructions:
            if isinstance(instr, _lc.CallOrInvokeInstruction):
                yield instr

CUDA_MATH_INTRINSICS_2 = {
    'llvm.exp.f32': ptx.exp_f32,
    'llvm.exp.f64': ptx.exp_f64,
    'fabsf'       : ptx.fabs_f32,
    'fabs'        : ptx.fabs_f64,
    'llvm.log.f32': ptx.log_f32,
    'llvm.log.f64': ptx.log_f64,
    'llvm.pow.f32': ptx.pow_f32,
    'llvm.pow.f64': ptx.pow_f64,
}

CUDA_MATH_INTRINSICS_3 = CUDA_MATH_INTRINSICS_2.copy()
CUDA_MATH_INTRINSICS_3.update({
                              # intentionally empty
})


def _link_llvm_math_intrinsics(module, cc):
    '''Discover and implement llvm math intrinsics that are not supported
    by NVVM.  NVVM only supports llvm.sqrt at this point (11/1/2012).
    '''
    to_be_implemented = {}   # new-function object -> inline ptx object
    to_be_removed = set()    # functions to be deleted
    inlinelist = []          # functions to be inlined

    library = {
        2 : CUDA_MATH_INTRINSICS_2,
        3 : CUDA_MATH_INTRINSICS_3,
    }[cc]

    # find all known math intrinsics and implement them.
    for lfunc in module.functions:
        for instr in _list_callinstr(lfunc):
            fn = instr.called_function
            if fn is not None: # maybe a inline asm
                fname = fn.name
                if fname in library:
                    inlinelist.append(instr)
                    to_be_removed.add(fn)
                    ftype = fn.type.pointee
                    newfn = module.get_or_insert_function(ftype, "numbapro.%s" % fname)
                    ptxcode = library[fname]
                    to_be_implemented[newfn] = ptxcode
                    instr.called_function = newfn  # replace the function
                else:
                    logger.debug("Unknown LLVM intrinsic %s", fname)

    # implement all the math functions with inline ptx
    for fn, ptx in to_be_implemented.items():
        entry = fn.append_basic_block('entry')
        builder = _lc.Builder.new(entry)
        value = builder.call(ptx, fn.args)
        builder.ret(value)
        to_be_removed.add(fn)

    # inline all the functions
    for callinstr in inlinelist:
        ok = _lc.inline_function(callinstr)
        assert ok

    for fn in to_be_removed:
        fn.delete()

def _temp_fix_to_remove_python_specifics(lfunc):
    inlinelist = []  # list of calls to be inlined
    fakepy = {}      # function name -> function object

    # find every call to python functions
    for instr in _list_callinstr(lfunc):
        fn = instr.called_function
        if fn is not None: # maybe a inline asm
            fname = fn.name
            if fname.startswith('Py_'):
                inlinelist.append(instr)
                fty = instr.called_function.type.pointee
                fakepy[fname] = fn
                assert fty.return_type == _lc.Type.void(), 'assume no sideeffect'
    # generate stub implementation for python functions
    # assumes that it returns void
    for fname, fn in fakepy.items():
        bldr = _lc.Builder.new(fn.append_basic_block('entry'))
        bldr.ret_void()

    # inline all the calls
    for call in inlinelist:
        ok = _lc.inline_function(call)
        assert ok

    # remove the stub from the module
    for fn in fakepy.values():
        fn.delete()


def _link_device_function(lfunc):
    toinline = []
    for instr in  _list_callinstr(lfunc):
        fn = instr.called_function
        if fn is not None and fn.is_declaration: # can be None for inline asm
            bag = _device_functions.get(fn.name)
            if bag is not None:
                pyfunc, linkee =bag
                lfunc.module.link_in(linkee.module.clone())
                if pyfunc._numba_inline:
                    toinline.append(instr)

    for call in toinline:
        callee = call.called_function
        _lc.inline_function(call)



class CudaDeviceFunction(numba.decorators.NumbaFunction):
    def __init__(self, py_func, wrapper=None, ctypes_func=None,
                 signature=None, lfunc=None, extra=None):
        super(CudaDeviceFunction, self).__init__(py_func, wrapper, ctypes_func,
                                                signature, lfunc)

    def __call__(self, *args, **kws):
        raise TypeError("")

class CudaBaseFunction(numba.decorators.NumbaFunction):

    _griddim = 1, 1, 1      # default grid dimension
    _blockdim = 1, 1, 1     # default block dimension
    _stream = 0
    def configure(self, griddim, blockdim, stream=0):
        '''Returns a new instance that is configured with the
        specified kernel grid dimension and block dimension.

        griddim, blockdim -- Triples of at most 3 integers. Missing dimensions
                             are automatically filled with `1`.

        '''
        import copy
        inst = copy.copy(self) # clone the object

        inst._griddim = griddim
        inst._blockdim = blockdim

        while len(inst._griddim) < 3:
            inst._griddim += (1,)

        while len(inst._blockdim) < 3:
            inst._blockdim += (1,)

        inst._stream = stream

        return inst

    def __getitem__(self, args):
        '''Shorthand for self.configure()
        '''
        return self.configure(*args)


def _generate_ptx(module, kernels):
    from numbapro._cuda import nvvm, default
    cc_major = default.device.COMPUTE_CAPABILITY[0]

    for kernel in kernels:
        _link_device_function(kernel)
        nvvm.set_cuda_kernel(kernel)
    _link_llvm_math_intrinsics(module, cc_major)

    arch = 'compute_%d0' % cc_major

    nvvm.fix_data_layout(module)

    pmb = _lp.PassManagerBuilder.new()
    pmb.opt_level = 2 # O3 causes bar.sync to be duplicated in unrolled loop
    pm = _lp.PassManager.new()
    pmb.populate(pm)
    pm.run(module)

    return nvvm.llvm_to_ptx(str(module), arch=arch)


class CudaNumbaFunction(CudaBaseFunction):
    def __init__(self, py_func, wrapper=None, ctypes_func=None,
                 signature=None, lfunc=None, extra=None):
        super(CudaNumbaFunction, self).__init__(py_func, wrapper, ctypes_func,
                                                signature, lfunc)
        # print 'translating...'
        self.module = lfunc.module
        self.extra = extra

        func_name = lfunc.name

        # FIXME: this function is called multiple times on the same lfunc.
        #        As a result, it has a long list of nvvm.annotation of the
        #        same data.

        self._ptxasm = _generate_ptx(lfunc.module, [lfunc])
                
        from numbapro import _cudadispatch
        self.dispatcher = _cudadispatch.CudaNumbaFuncDispatcher(self._ptxasm,
                                                                func_name,
                                                                lfunc.type)

    def __call__(self, *args):
        '''Call the CUDA kernel.

        This call is synchronous to the host.
        In another words, this function will return only upon the completion
        of the CUDA kernel.
        '''
        return self.dispatcher(args, self._griddim, self._blockdim,
                               stream=self._stream)

    #    @property
    #    def compute_capability(self):
    #        '''The compute_capability of the PTX is generated for.
    #        '''
    #        return self._cc

    #    @property
    #    def target_machine(self):
    #        '''The LLVM target mcahine backend used to generate the PTX.
    #        '''
    #        return self._arch
    #
    @property
    def ptx(self):
        '''Returns the PTX assembly for this function.
        '''
        return self._ptxasm

    @property
    def device(self):
        return self.dispatcher.device

class CudaAutoJitNumbaFunction(CudaBaseFunction):

    def invoke_compiled(self, compiled_cuda_func, *args, **kwargs):
        return compiled_cuda_func[self._griddim, self._blockdim](*args, **kwargs)

#class CudaTranslate(_Translate):
#     def op_LOAD_ATTR(self, i, op, arg):
#        '''Add cuda intrinsics lookup.
#        '''
#        peekarg = self.stack[-1]
#        if peekarg.val in _ATTRIBUTABLES:
#            objarg = self.stack.pop(-1)
#            res = getattr(objarg.val, self.names[arg])
#            if res in _SPECIAL_VALUES:
#                intr_func_bldr = _sreg(_SPECIAL_VALUES[res])
#                intr = intr_func_bldr(self.mod)
#                res = self.builder.call(intr, [])
#            self.stack.append(_Variable(res))
#        else:
#            # fall back to default implementation
#            super(CudaTranslate, self).op_LOAD_ATTR(i, op, arg)

# Patch numba
numba.decorators.jit_targets[('gpu', 'ast')] = jit2 # give up on bytecode path
numba.decorators.numba_function_autojit_targets['gpu'] = CudaAutoJitNumbaFunction


# NDarray device helper
def to_device(ary, *args, **kws):
    from numbapro._cuda import devicearray
    devarray =  ary.view(type=devicearray.DeviceNDArray)
    devarray.to_device(*args, **kws)
    return devarray

# Stream helper

def stream():
    from numbapro._cuda.driver import Stream
    return Stream()

# Macros

