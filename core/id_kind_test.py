#!/usr/bin/env python2
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
id_kind_test.py: Tests for id_kind.py
"""
from __future__ import print_function

import unittest

from _devbuild.gen.id_kind_asdl import Id, Kind
from _devbuild.gen import syntax_asdl 
from core import id_kind
from core.meta import (
    IdInstance, LookupKind, ID_SPEC, BOOL_ARG_TYPES, _kind_sizes
)


class TokensTest(unittest.TestCase):

  def testId(self):
    print(dir(Id))
    print(Id.Op_Newline)
    print(Id.Undefined_Tok)

  def testTokens(self):
    print(Id.Op_Newline)
    print(syntax_asdl.token(Id.Op_Newline, '\n'))

    print(Id.Op_Newline)

    print(Kind.Eof)
    print(Kind.Left)
    print('--')
    num_kinds = 0
    for name in dir(Kind):
      if name[0].isupper():
        print(name, getattr(Kind, name))
        num_kinds += 1

    print('Number of Kinds:', num_kinds)
    # 233 out of 256 tokens now
    print('Number of IDs:', len(ID_SPEC.id_str2int))

    # Make sure we're not exporting too much
    print(dir(id_kind))

    t = syntax_asdl.token(Id.Arith_Plus, '+')
    self.assertEqual(Kind.Arith, LookupKind(t.id))
    t = syntax_asdl.token(Id.Arith_CaretEqual, '^=')
    self.assertEqual(Kind.Arith, LookupKind(t.id))
    t = syntax_asdl.token(Id.Arith_RBrace, '}')
    self.assertEqual(Kind.Arith, LookupKind(t.id))

    t = syntax_asdl.token(Id.BoolBinary_GlobDEqual, '==')
    self.assertEqual(Kind.BoolBinary, LookupKind(t.id))

    t = syntax_asdl.token(Id.BoolBinary_Equal, '=')
    self.assertEqual(Kind.BoolBinary, LookupKind(t.id))

  def testEquality(self):
    left = IdInstance(198)
    right = IdInstance(198)
    print(left, right)
    print(left == right)
    self.assertEqual(left, right)

  def testLexerPairs(self):
    def MakeLookup(p):
      return dict((pat, tok) for _, pat, tok in p)

    lookup = MakeLookup(ID_SPEC.LexerPairs(Kind.BoolUnary))
    print(lookup)
    self.assertEqual(Id.BoolUnary_e, lookup['-e'])
    self.assertEqual(Id.BoolUnary_z, lookup['-z'])

    lookup2 = MakeLookup(ID_SPEC.LexerPairs(Kind.BoolBinary))
    self.assertEqual(Id.BoolBinary_eq, lookup2['-eq'])
    #print(lookup2)

  def testPrintStats(self):
    k = _kind_sizes
    print('STATS: %d tokens in %d groups: %s' % (sum(k), len(k), k))
    # Thinking about switching
    big = [i for i in k if i > 8]
    print('%d BIG groups: %s' % (len(big), sorted(big)))

    PrintBoolTable()


def PrintBoolTable():
  for i, arg_type in BOOL_ARG_TYPES.items():
    row = (IdInstance(i), arg_type)
    print('\t'.join(str(c) for c in row))


if __name__ == '__main__':
  unittest.main()
