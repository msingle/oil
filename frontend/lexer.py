# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
lexer.py - Library for lexing.
"""

from _devbuild.gen.syntax_asdl import token, line_span
from _devbuild.gen.types_asdl import lex_mode_t
from _devbuild.gen.id_kind_asdl import Id_t, Id
from asdl import const
from core.util import log

from typing import Callable, List, Tuple, TYPE_CHECKING
if TYPE_CHECKING:
  from core.alloc import Arena
  from frontend.reader import _Reader
  from frontend.match import MatchFunc


def C(pat, tok_type):
  # type: (str, Id_t) -> Tuple[bool, str, Id_t]
  """ Lexer rule with a constant string, e.g. C('$*', VSub_Star) """
  return (False, pat, tok_type)


def R(pat, tok_type):
  # type: (str, Id_t) -> Tuple[bool, str, Id_t]
  """ Lexer rule with a regex string, e.g. R('\$[0-9]', VSub_Number) """
  return (True, pat, tok_type)


class LineLexer(object):
  def __init__(self, match_func, line, arena):
    # type: (MatchFunc, str, Arena) -> None
    self.match_func = match_func
    self.arena = arena

    self.arena_skip = False  # For MaybeUnreadOne
    self.last_span_id = const.NO_INTEGER  # For MaybeUnreadOne

    self.Reset(line, -1, 0)  # Invalid line_id to start

  def __repr__(self):
    # type: () -> str
    return '<LineLexer at pos %d of line %r (id = %d)>' % (
        self.line_pos, self.line, self.line_id)

  def Reset(self, line, line_id, line_pos):
    # type: (str, int, int) -> None
    #assert line, repr(line)  # can't be empty or None
    self.line = line
    self.line_id = line_id
    self.line_pos = line_pos

  def MaybeUnreadOne(self):
    # type: () -> bool
    """Return True if we can unread one character, or False otherwise.

    NOTE: Only call this when you know the last token was exactly on character!
    """
    if self.line_pos == 0:
      return False
    else:
      self.line_pos -= 1
      self.arena_skip = True  # don't add the next token to the arena
      return True

  def GetSpanIdForEof(self):
    # type: () -> int
    """Create a new span ID for syntax errors involving the EOF token."""
    if self.line_id == -1:
      # When line_id == -1, this means there are ZERO lines.  Add a dummy line
      # 0 so the span_id has a source to display errors.
      line_id = self.arena.AddLine('', 0)
    else:
      line_id = self.line_id
    return self.arena.AddLineSpan(line_id, self.line_pos, 0)

  def LookAhead(self, lex_mode):
    # type: (lex_mode_t) -> token
    """Look ahead for a non-space token, using the given lexer mode.

    Does NOT advance self.line_pos.

    Called with at least the following modes:
      lex_mode_e.Arith -- for ${a[@]} vs ${a[1+2]}
      lex_mode_e.VSub_1
      lex_mode_e.ShCommand
    """
    pos = self.line_pos
    n = len(self.line)
    #print('Look ahead from pos %d, line %r' % (pos,self.line))
    while True:
      if pos == n:
        # We don't allow lookahead while already at end of line, because it
        # would involve interacting with the line reader, and we never need
        # it.  In the OUTER mode, there is an explicit newline token, but
        # ARITH doesn't have it.
        t = token(Id.Unknown_Tok, '', const.NO_INTEGER)
        return t

      tok_type, end_pos = self.match_func(lex_mode, self.line, pos)
      tok_val = self.line[pos:end_pos]
      # NOTE: Instead of hard-coding this token, we could pass it in.  This
      # one only appears in OUTER state!  LookAhead(lex_mode, past_token_type)
      if tok_type != Id.WS_Space:
        break
      pos = end_pos

    return token(tok_type, tok_val, const.NO_INTEGER)

  def Read(self, lex_mode):
    # type: (lex_mode_t) -> token
    # Inner loop optimization
    line = self.line
    line_pos = self.line_pos

    tok_type, end_pos = self.match_func(lex_mode, line, line_pos)
    if tok_type == Id.Eol_Tok:  # Do NOT add a span for this sentinel!
      return token(tok_type, '', const.NO_INTEGER)

    tok_val = line[line_pos:end_pos]

    # NOTE: We're putting the arena hook in LineLexer and not Lexer because we
    # want it to be "low level".  The only thing fabricated here is a newline
    # added at the last line, so we don't end with \0.

    if self.arena_skip:  # make another token from the last span
      assert self.last_span_id != const.NO_INTEGER
      span_id = self.last_span_id
      self.arena_skip = False
    else:
      span_id = self.arena.AddLineSpan(self.line_id, line_pos, len(tok_val))
      self.last_span_id = span_id
    #log('LineLexer.Read() span ID %d for %s', span_id, tok_type)

    t = token(tok_type, tok_val, span_id)
    self.line_pos = end_pos
    return t


class Lexer(object):
  """
  Read lines from the line_reader, split them into tokens with line_lexer,
  returning them in a stream.
  """
  def __init__(self, line_lexer, line_reader):
    # type: (LineLexer, _Reader) -> None
    """
    Args:
      line_lexer: Underlying object to get tokens from
      line_reader: get new lines from here
    """
    self.line_lexer = line_lexer
    self.line_reader = line_reader
    self.line_id = -1  # Invalid one
    self.translation_stack = []  # type: List[Tuple[Id_t, Id_t]]
    self.emit_comp_dummy = False

  def ResetInputObjects(self):
    # type: () -> None
    self.line_lexer.Reset('', -1, 0)

  def MaybeUnreadOne(self):
    # type: () -> bool
    return self.line_lexer.MaybeUnreadOne()

  def LookAhead(self, lex_mode):
    # type: (lex_mode_t) -> token
    """Look ahead in the current line for the next non-space token.

    NOTE: Limiting lookahead to the current line makes the code a lot simpler
    at the cost of a rare (and superfluous) corner case.  This is not
    recognized as a function:

    foo\
    () {}

    Of course, this is the proper way to break it:

    foo()
    {}
    """
    return self.line_lexer.LookAhead(lex_mode)

  def EmitCompDummy(self):
    # type: () -> None
    """Emit Id.Lit_CompDummy right before EOF, for completion."""
    self.emit_comp_dummy = True

  def PushHint(self, old_id, new_id):
    # type: (Id_t, Id_t) -> None
    """
    Use cases:
    Id.Op_RParen -> Id.Right_Subshell -- disambiguate
    Id.Op_RParen -> Id.Eof_RParen

    Problems for $() nesting.

    - posix:
      - case foo) and case (foo)
      - func() {}
      - subshell ( )
    - bash extensions:
      - precedence in [[,   e.g.  [[ (1 == 2) && (2 == 3) ]]
      - arrays: a=(1 2 3), a+=(4 5)
    """
    self.translation_stack.append((old_id, new_id))

  def _Read(self, lex_mode):
    # type: (lex_mode_t) -> token
    """Read from the normal line buffer, not an alias."""
    t = self.line_lexer.Read(lex_mode)
    if t.id == Id.Eol_Tok:  # hit \0, read a new line
      line_id, line, line_pos = self.line_reader.GetLine()

      if line is None:  # no more lines
        span_id = self.line_lexer.GetSpanIdForEof()
        if self.emit_comp_dummy:
          id_ = Id.Lit_CompDummy
          self.emit_comp_dummy = False  # emit EOF the next time
        else:
          id_ = Id.Eof_Real
        t = token(id_, '', span_id)
        return t

      self.line_lexer.Reset(line, line_id, line_pos)  # fill with a new line
      t = self.line_lexer.Read(lex_mode)

    # e.g. translate ) or ` into EOF
    if self.translation_stack:
      old_id, new_id = self.translation_stack[-1]  # top
      if t.id == old_id:
        #print('==> TRANSLATING %s ==> %s' % (t, new_s))
        self.translation_stack.pop()
        t.id = new_id

    return t

  def Read(self, lex_mode):
    # type: (lex_mode_t) -> token
    while True:
      t = self._Read(lex_mode)
      # TODO: Change to ALL IGNORED types, once you have SPACE_TOK.  This means
      # we don't have to handle them in the VSub_1/VSub_2/etc. states.
      if t.id != Id.Ignored_LineCont:
        break

    #log('Read() Returning %s', t)
    return t
