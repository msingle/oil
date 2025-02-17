"""
gen_cpp.py

TODO:
- pretty printing methods
  - so asdl/format.py get translated?

- ASDL optional args to C++ default arguments?
- what about spids?  optional?
  - TODO: test this out in target_lang.cc

- NoOp needs to be instantiated without args?
- dict becomes Dict[str, str] ?
- how to handle UserType(id) ?

- How do optional ASDL values like int? work?  Use C++ default values?
  - This means that all the optionals have to be on the end.  That seems OK.
  - I guess that's how Python does it.
"""
from __future__ import print_function

import sys

from asdl import meta
from asdl import runtime
from asdl import visitor


# Used by core/asdl_gen.py to generate _devbuild/gen/osh-types.h, with
# lex_mode__*
class CEnumVisitor(visitor.AsdlVisitor):

  def VisitSimpleSum(self, sum, name, depth):
    # Just use #define, since enums aren't namespaced.
    for i, variant in enumerate(sum.types):
      self.Emit('#define %s__%s %d' % (name, variant.name, i + 1), depth)
    self.Emit("", depth)


_BUILTINS = {
    'string': 'Str*',  # declared in runtime.h
    'int': 'int',
    'bool': 'bool',
    #'id': 'Id',  # Application specific hack for now
}


class ForwardDeclareVisitor(visitor.AsdlVisitor):
  """Print forward declarations.

  ASDL allows forward references of types, but C++ doesn't.
  """
  def VisitCompoundSum(self, sum, name, depth):
    self.Emit("class %(name)s_t;" % locals(), depth)

  def VisitProduct(self, product, name, depth):
    self.Emit("class %(name)s_t;" % locals(), depth)

  def EmitFooter(self):
    self.Emit("", 0)  # blank line


class ClassDefVisitor(visitor.AsdlVisitor):
  """Generate C++ classes and type-safe enums."""

  def __init__(self, f, type_lookup):
    visitor.AsdlVisitor.__init__(self, f)
    self.type_lookup = type_lookup

  def _GetInnerCppType(self, field):
    type_name = field.type

    cpp_type = _BUILTINS.get(type_name)
    if cpp_type is not None:
      return cpp_type

    typ = self.type_lookup[type_name]
    if isinstance(typ, meta.SumType) and typ.is_simple:
      # Use the enum instead of the class.
      return "%s_e" % type_name

    # Everything is a pointer for now.  No references.
    return "%s_t*" % type_name

  def _GetCppType(self, field):
    """Return a string for the C++ name of the type."""

    # TODO: The once instance of 'dict' needs an overhaul!
    # Right now it's untyped in ASDL.
    # I guess is should be Dict[str, str] for the associative array contents?
    c_type = self._GetInnerCppType(field)
    if field.seq:
      return 'List<%s>*' % c_type
    else:
      return c_type

  def _EmitEnum(self, sum, name, depth):
    enum = []
    for i in xrange(len(sum.types)):
      type = sum.types[i]
      enum.append("%s = %d" % (type.name, i + 1))  # zero is reserved

    self.Emit("enum class %s_e {" % name, depth)
    self.Emit(", ".join(enum), depth + 1)
    self.Emit("};", depth)
    self.Emit("", depth)

  def VisitSimpleSum(self, sum, name, depth):
    self._EmitEnum(sum, name, depth)
    # type alias to match Python code
    self.Emit('typedef %s_e %s_t;' % (name, name), depth)

  def VisitCompoundSum(self, sum, name, depth):
    # This is a sign that Python needs string interpolation!!!
    def Emit(s, depth=depth):
      self.Emit(s % sys._getframe(1).f_locals, depth)

    self._EmitEnum(sum, name, depth)

    # TODO: DISALLOW_COPY_AND_ASSIGN on this class and others?

    # This is the base class.
    Emit("class %(name)s_t {")
    Emit(" public:")
    Emit("  %s_t(%s_e tag) : tag(tag) {" % (name, name))
    Emit("  }")
    Emit("  %s_e tag;" % name)
    Emit("};")
    Emit("")

    for t in sum.types:
      self.VisitConstructor(t, name, depth)

    # rudimentary attribute handling
    for field in sum.attributes:
      type_name = str(field.type)
      assert type_name in runtime.BUILTIN_TYPES, type_name
      Emit("%s %s;" % (type_name, field.name), depth + 1)

  def _GenClass(self, cons, class_name, super_name, depth, tag=None):
    """For Product and Constructor."""
    if super_name:
      self.Emit("class %s : public %s {" % (class_name, super_name), depth)
    else:
      self.Emit("class %s {" % class_name, depth)
    self.Emit(" public:", depth)

    params = []
    inits = []

    if tag:
      inits.append('%s(%s)' % (super_name, tag))
    for f in cons.fields:
      if f.name == 'spids':
        continue
      params.append('%s %s' % (self._GetCppType(f), f.name))
      inits.append('%s(%s)' % (f.name, f.name))

    # Constructor
    self.Emit("  %s(%s) : %s {" %
        (class_name, ', '.join(params), ', '.join(inits)), depth)
    self.Emit("  }")

    for f in cons.fields:
      self.Emit("  %s %s;" % (self._GetCppType(f), f.name))
    self.Emit("};", depth)
    self.Emit("", depth)

  def VisitConstructor(self, cons, sum_name, depth):
    #print(dir(cons))
    super_name = '%s_t' % sum_name
    if cons.fields:
      tag = '%s_e::%s' % (sum_name, cons.name)
      class_name = '%s__%s' % (sum_name, cons.name)
      self._GenClass(cons, class_name, super_name, depth, tag=tag)

  def VisitProduct(self, product, name, depth):
    self._GenClass(product, name, None, depth)

    for field in product.attributes:
      # rudimentary attribute handling
      type_name = str(field.type)
      assert type_name in runtime.BUILTIN_TYPES, type_name
      self.Emit("%s %s;" % (type_name, field.name), depth + 1)
