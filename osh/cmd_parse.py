# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
cmd_parse.py - Parse high level shell commands.
"""
from __future__ import print_function

from _devbuild.gen.id_kind_asdl import Id, Kind, Id_t
from _devbuild.gen.types_asdl import lex_mode_t, lex_mode_e
from _devbuild.gen.syntax_asdl import (
    command, command_e, command_t,
    command__Assignment, command__SimpleCommand, command__BraceGroup,
    command__DoGroup, command__ForExpr, command__ForEach, command__WhileUntil,
    command__Case, command__If, command__FuncDef, command__Subshell,
    command__DBracket, command__DParen, command__CommandList,
    case_arm,

    lhs_expr, lhs_expr_t,
    redir, redir_t, redir__HereDoc,
    word_t, word__CompoundWord, word__TokenWord,
    word_part, word_part_t, word_part__LiteralPart,

    token, assign_pair, env_pair,
    assign_op_e,

    source,
    parse_result, parse_result_t,
)
from _devbuild.gen.syntax_asdl import word as osh_word  # TODO: rename
from _devbuild.gen import syntax_asdl  # line_span

from asdl import const
from core import util
from core.util import log, p_die
from frontend import match
from frontend import reader
from osh import braces
from osh import bool_parse
from osh import word

from typing import Optional, List, Tuple, cast, TYPE_CHECKING
if TYPE_CHECKING:
  from core.alloc import Arena
  from frontend.lexer import Lexer
  from frontend.parse_lib import ParseContext, AliasesInFlight
  from frontend.reader import _Reader
  from osh.word_parse import WordParser


def _ReadHereLines(line_reader,  # type: _Reader
                   h,  # type: redir__HereDoc
                   delimiter,  # type: str
                   ):
  # type: (...) -> Tuple[List[Tuple[int, str, int]], Tuple[int, str, int]]
  # NOTE: We read all lines at once, instead of parsing line-by-line,
  # because of cases like this:
  # cat <<EOF
  # 1 $(echo 2
  # echo 3) 4
  # EOF
  here_lines = []
  last_line = None
  strip_leading_tabs = (h.op.id == Id.Redir_DLessDash)

  while True:
    line_id, line, unused_offset = line_reader.GetLine()

    if not line:  # EOF
      # An unterminated here doc is just a warning in bash.  We make it
      # fatal because we want to be strict, and because it causes problems
      # reporting other errors.
      # Attribute it to the << in <<EOF for now.
      p_die("Couldn't find terminator for here doc that starts here",
            token=h.op)

    # If op is <<-, strip off ALL leading tabs -- not spaces, and not just
    # the first tab.
    start_offset = 0
    if strip_leading_tabs:
      n = len(line)
      i = 0  # used after loop exit
      while i < n:
        if line[i] != '\t':
          break
        i += 1
      start_offset = i

    if line[start_offset:].rstrip() == delimiter:
      last_line = (line_id, line, start_offset)
      break

    here_lines.append((line_id, line, start_offset))

  return here_lines, last_line


def _MakeLiteralHereLines(here_lines,  # type: List[Tuple[int, str, int]]
                          arena,  # type: Arena
                          ):
  # type: (...) -> List[word_part_t]  # less precise because List is invariant type
  """Create a line_span and a token for each line."""
  tokens = []
  for line_id, line, start_offset in here_lines:
    span_id = arena.AddLineSpan(line_id, start_offset, len(line))
    t = syntax_asdl.token(Id.Lit_Chars, line[start_offset:], span_id)
    tokens.append(t)
  return [word_part.LiteralPart(t) for t in tokens]


def _ParseHereDocBody(parse_ctx, h, line_reader, arena):
  # type: (ParseContext, redir__HereDoc, _Reader, Arena) -> None
  """Fill in attributes of a pending here doc node."""
  # "If any character in word is quoted, the delimiter shall be formed by
  # performing quote removal on word, and the here-document lines shall not
  # be expanded. Otherwise, the delimiter shall be the word itself."
  # NOTE: \EOF counts, or even E\OF
  ok, delimiter, delim_quoted = word.StaticEval(h.here_begin)
  if not ok:
    p_die('Invalid here doc delimiter', word=h.here_begin)

  here_lines, last_line = _ReadHereLines(line_reader, h, delimiter)

  if delim_quoted:  # << 'EOF'
    # LiteralPart for each line.
    h.stdin_parts = _MakeLiteralHereLines(here_lines, arena)
  else:
    line_reader = reader.VirtualLineReader(here_lines, arena)
    w_parser = parse_ctx.MakeWordParserForHereDoc(line_reader)
    w_parser.ReadHereDocBody(h.stdin_parts)  # fills this in

  end_line_id, end_line, end_pos = last_line

  # Create a span with the end terminator.  Maintains the invariant that
  # the spans "add up".
  h.here_end_span_id = arena.AddLineSpan(end_line_id, end_pos, len(end_line))


def _MakeAssignPair(parse_ctx,  # type: ParseContext
                    preparsed,  # type: Tuple[token, Optional[token], int, word__CompoundWord]
                    arena,  # type: Arena
                    ):
  # type: (...) -> assign_pair
  """Create an assign_pair from a 4-tuples from DetectAssignment."""

  left_token, close_token, part_offset, w = preparsed

  if left_token.id == Id.Lit_VarLike:  # s=1
    if left_token.val[-2] == '+':
      var_name = left_token.val[:-2]
      op = assign_op_e.PlusEqual
    else:
      var_name = left_token.val[:-1]
      op = assign_op_e.Equal

    lhs_= lhs_expr.LhsName(var_name)
    lhs_.spids.append(left_token.span_id)

    lhs = cast(lhs_expr_t, lhs_)  # for MyPy

  elif left_token.id == Id.Lit_ArrayLhsOpen and parse_ctx.one_pass_parse:
    var_name = left_token.val[:-1]
    if close_token.val[-2] == '+':
      op = assign_op_e.PlusEqual
    else:
      op = assign_op_e.Equal

    left_spid = left_token.span_id + 1
    right_spid = close_token.span_id

    left_span = parse_ctx.arena.GetLineSpan(left_spid)
    right_span = parse_ctx.arena.GetLineSpan(right_spid)
    assert left_span.line_id == right_span.line_id, \
        '%s and %s not on same line' % (left_span, right_span)

    line = parse_ctx.arena.GetLine(left_span.line_id)
    index_str = line[left_span.col : right_span.col]
    lhs = lhs_expr.CompatIndexedName(var_name, index_str)

  elif left_token.id == Id.Lit_ArrayLhsOpen:  # a[x++]=1
    var_name = left_token.val[:-1]
    if close_token.val[-2] == '+':
      op = assign_op_e.PlusEqual
    else:
      op = assign_op_e.Equal

    spid1 = left_token.span_id
    spid2 = close_token.span_id
    span1 = arena.GetLineSpan(spid1)
    span2 = arena.GetLineSpan(spid2)
    if span1.line_id == span2.line_id:
      line = arena.GetLine(span1.line_id)
      # extract what's between brackets
      code_str = line[span1.col + span1.length : span2.col]
    else:
      raise NotImplementedError('%d != %d' % (span1.line_id, span2.line_id))
    a_parser = parse_ctx.MakeArithParser(code_str)
    arena.PushSource(source.LValue(left_token.span_id, close_token.span_id))
    try:
      index_node = a_parser.Parse()  # may raise util.ParseError
    finally:
      arena.PopSource()
    lhs = lhs_expr.LhsIndexedName(var_name, index_node)
    lhs.spids.append(left_token.span_id)

  else:
    raise AssertionError

  # TODO: Should we also create a rhs_expr.ArrayLiteral here?
  n = len(w.parts)
  if part_offset == n:
    val = osh_word.EmptyWord()  # type: word_t
  else:
    val = osh_word.CompoundWord(w.parts[part_offset:])
    val = word.TildeDetect(val) or val

  pair = syntax_asdl.assign_pair(lhs, op, val)
  pair.spids.append(left_token.span_id)  # To skip to beginning of pair
  return pair


def _AppendMoreEnv(preparsed_list, more_env):
  # type: (PreParsedList, List[env_pair]) -> None
  """Helper to modify a SimpleCommand node.

  Args:
    preparsed: a list of 4-tuples from DetectAssignment
    more_env: a list to append env_pairs to
  """

  for left_token, close_token, part_offset, w in preparsed_list:
    if left_token.id != Id.Lit_VarLike:  # can't be a[x]=1
      p_die("Environment binding shouldn't look like an array assignment",
            token=left_token)

    if left_token.val[-2] == '+':
      p_die('Expected = in environment binding, got +=', token=left_token)

    var_name = left_token.val[:-1]
    n = len(w.parts)
    if part_offset == n:
      val = osh_word.EmptyWord()  # type: word_t
    else:
      val = osh_word.CompoundWord(w.parts[part_offset:])

    pair = syntax_asdl.env_pair(var_name, val)
    pair.spids.append(left_token.span_id)  # Do we need this?

    more_env.append(pair)


def _MakeAssignment(parse_ctx,  # type: ParseContext
                    assign_kw,  # type: Id_t
                    suffix_words  # type: List[word__CompoundWord]
                    ):
  # type: (...) -> command__Assignment
  """Create an command.Assignment node from a keyword and a list of words.

  NOTE: We don't allow dynamic assignments like:

  local $1

  This can be replaced with eval 'local $1'
  """
  # First parse flags, e.g. -r -x -a -A.  None of the flags have arguments.
  flags = []
  n = len(suffix_words)
  i = 1
  while i < n:
    w = suffix_words[i]
    ok, static_val, quoted = word.StaticEval(w)
    if not ok or quoted:
      break  # can't statically evaluate

    if static_val.startswith('-'):
      flags.append(static_val)
    else:
      break  # not a flag, rest are args
    i += 1

  # Now parse bindings or variable names
  pairs = []
  while i < n:
    w = suffix_words[i]
    # declare x[y]=1 is valid
    left_token, close_token, part_offset = word.DetectAssignment(w)
    if left_token:
      preparsed = (left_token, close_token, part_offset, w)
      pair = _MakeAssignPair(parse_ctx, preparsed, parse_ctx.arena)
    else:
      # In aboriginal in variables/sources: export_if_blank does export "$1".
      # We should allow that.

      # Parse this differently then?  # dynamic-export?  It sets global
      # variables.
      ok, static_val, quoted = word.StaticEval(w)
      if not ok or quoted:
        p_die("Variable names must be unquoted constants", word=w)

      # No value is equivalent to ''
      if not match.IsValidVarName(static_val):
        p_die('Invalid variable name %r', static_val, word=w)

      lhs = lhs_expr.LhsName(static_val)
      lhs.spids.append(word.LeftMostSpanForWord(w))
      pair = syntax_asdl.assign_pair(lhs, assign_op_e.Equal, None)

      left_spid = word.LeftMostSpanForWord(w)
      pair.spids.append(left_spid)
    pairs.append(pair)

    i += 1

  node = command.Assignment(assign_kw, flags, pairs)
  return node


if TYPE_CHECKING:
  PreParsedList = List[Tuple[token, Optional[token], int, word__CompoundWord]]

def _SplitSimpleCommandPrefix(words  # type: List[word__CompoundWord]
                              ):
  # type: (...) -> Tuple[PreParsedList, List[word__CompoundWord]]
  """Second pass of SimpleCommand parsing: look for assignment words."""
  preparsed_list = []
  suffix_words = []

  done_prefix = False
  for w in words:
    if done_prefix:
      suffix_words.append(w)
      continue

    left_token, close_token, part_offset = word.DetectAssignment(w)
    if left_token:
      preparsed_list.append((left_token, close_token, part_offset, w))
    else:
      done_prefix = True
      suffix_words.append(w)

  return preparsed_list, suffix_words


def _MakeSimpleCommand(preparsed_list, suffix_words, redirects):
  # type: (PreParsedList, List[word__CompoundWord], List[redir_t]) -> command__SimpleCommand
  """Create an command.SimpleCommand node."""

  # FOO=(1 2 3) ls is not allowed.
  for _, _, _, w in preparsed_list:
    if word.HasArrayPart(w):
      p_die("Environment bindings can't contain array literals", word=w)

  # echo FOO=(1 2 3) is not allowed (but we should NOT fail on echo FOO[x]=1).
  for w in suffix_words:
    if word.HasArrayPart(w):
      p_die("Commands can't contain array literals", word=w)

  # NOTE: We only do brace DETECTION here, not brace EXPANSION.  Therefore we
  # can't implement bash's behavior of having say {~bob,~jane}/src work,
  # because we only have a BracedWordTree.
  # This is documented in spec/brace-expansion.
  # NOTE: Technically we could do expansion outside of 'oshc translate', but it
  # doesn't seem worth it.
  words2 = braces.BraceDetectAll(suffix_words)
  words3 = word.TildeDetectAll(words2)

  node = command.SimpleCommand()
  node.words = words3
  node.redirects = redirects
  _AppendMoreEnv(preparsed_list, node.more_env)
  return node


NOT_FIRST_WORDS = (
    Id.KW_Do, Id.KW_Done, Id.KW_Then, Id.KW_Fi, Id.KW_Elif,
    Id.KW_Else, Id.KW_Esac
)


class CommandParser(object):
  """
  Args:
    word_parse: to get a stream of words
    lexer: for lookahead in function def, PushHint of ()
    line_reader: for here doc
    eof_id: for command subs
    arena: where to add nodes, spans, lines, etc.
    aliases_in_flight: for preventing infinite alias expansion
  """
  def __init__(self,
               parse_ctx,  # type: ParseContext
               w_parser,  # type: WordParser
               lexer,  # type: Lexer
               line_reader,  # type: _Reader
               eof_id=Id.Eof_Real,  # type: Id_t
               aliases_in_flight=None,  # type: Optional[AliasesInFlight]
               ):
    # type: (...) -> None
    self.parse_ctx = parse_ctx
    self.aliases = parse_ctx.aliases  # aliases to expand at parse time

    self.w_parser = w_parser  # type: WordParser  # for normal parsing
    self.lexer = lexer  # for pushing hints, lookahead to (
    self.line_reader = line_reader  # for here docs
    self.arena = parse_ctx.arena  # for adding here doc and alias spans
    self.eof_id = eof_id
    self.aliases_in_flight = aliases_in_flight

    self.Reset()

  def Reset(self):
    # type: () -> None
    """Reset our own internal state.

    Called by the interactive loop.
    """
    # Cursor state set by _Peek()
    self.next_lex_mode = lex_mode_e.ShCommand
    self.cur_word = None  # type: word_t  # current word
    self.c_kind = Kind.Undefined
    self.c_id = Id.Undefined_Tok

    self.pending_here_docs = []  # type: List[redir__HereDoc]

  def ResetInputObjects(self):
    # type: () -> None
    """Reset the internal state of our inputs.

    Called by the interactive loop.
    """
    self.w_parser.Reset()
    self.lexer.ResetInputObjects()
    self.line_reader.Reset()

  def _Next(self, lex_mode=lex_mode_e.ShCommand):
    # type: (lex_mode_t) -> None
    """Helper method."""
    self.next_lex_mode = lex_mode

  def _Peek(self):
    # type: () -> None
    """Helper method.

    Returns True for success and False on error.  Error examples: bad command
    sub word, or unterminated quoted string, etc.
    """
    if self.next_lex_mode != lex_mode_e.Undefined:
      w = self.w_parser.ReadWord(self.next_lex_mode)

      # Here docs only happen in command mode, so other kinds of newlines don't
      # count.
      if isinstance(w, word__TokenWord) and w.token.id == Id.Op_Newline:
        for h in self.pending_here_docs:
          _ParseHereDocBody(self.parse_ctx, h, self.line_reader, self.arena)
        del self.pending_here_docs[:]  # No .clear() until Python 3.3.

      self.cur_word = w

      self.c_kind = word.CommandKind(self.cur_word)
      self.c_id = word.CommandId(self.cur_word)
      self.next_lex_mode = lex_mode_e.Undefined

  def _Eat(self, c_id):
    # type: (Id_t) -> None
    """Consume a word of a type.  If it doesn't match, return False.

    Args:
      c_id: either EKeyword.* or a token type like Id.Right_Subshell.
      TODO: Rationalize / type check this.
    """
    self._Peek()
    # TODO: Printing something like KW_Do is not friendly.  We can map
    # backwards using the _KEYWORDS list in osh/lex.py.
    if self.c_id != c_id:
      p_die('Expected word type %s, got %s', c_id,
            word.CommandId(self.cur_word), word=self.cur_word)

    self._Next()

  def _NewlineOk(self):
    # type: () -> None
    """Check for optional newline and consume it."""
    self._Peek()
    if self.c_id == Id.Op_Newline:
      self._Next()
      self._Peek()

  def ParseRedirect(self):
    # type: () -> redir_t
    """
    Problem: You don't know which kind of redir_node to instantiate before
    this?  You could stuff them all in one node, and then have a switch() on
    the type.

    You need different types.
    """
    self._Peek()
    assert self.c_kind == Kind.Redir, self.cur_word
    w = cast(word__TokenWord, self.cur_word)  # for MyPy

    op = w.token
    # For now only supporting single digit descriptor
    first_char = w.token.val[0]
    if first_char.isdigit():
      fd = int(first_char)
    else:
      fd = const.NO_INTEGER

    self._Next()
    self._Peek()

    # Here doc
    if op.id in (Id.Redir_DLess, Id.Redir_DLessDash):
      here_begin = self.cur_word
      self._Next()

      h = redir.HereDoc(op, fd, here_begin)
      self.pending_here_docs.append(h)  # will be filled on next newline.
      return h

    # Other redirect
    if self.c_kind != Kind.Word:
      p_die('Invalid token after redirect operator', word=self.cur_word)

    w2 = word.TildeDetect(self.cur_word)
    arg_word = w2 or self.cur_word
    self._Next()

    return redir.Redir(op, fd, arg_word)

  def _ParseRedirectList(self):
    # type: () -> List[redir_t]
    """Try parsing any redirects at the cursor.

    This is used for blocks only, not commands.

    Return None on error.
    """
    redirects = []
    while True:
      self._Peek()

      # This prediction needs to ONLY accept redirect operators.  Should we
      # make them a separate TokeNkind?
      if self.c_kind != Kind.Redir:
        break

      node = self.ParseRedirect()
      redirects.append(node)
      self._Next()
    return redirects

  def _ScanSimpleCommand(self):
    # type: () -> Tuple[List[redir_t], List[word__CompoundWord]]
    """First pass: Split into redirects and words."""
    redirects = []  # type: List[redir_t]
    words = []  # type: List[word__CompoundWord]
    while True:
      self._Peek()
      if self.c_kind == Kind.Redir:
        node = self.ParseRedirect()
        redirects.append(node)

      elif self.c_kind == Kind.Word:
        assert isinstance(self.cur_word, word__CompoundWord)  # for MyPy
        words.append(self.cur_word)

      else:
        break

      self._Next()
    return redirects, words

  def _MaybeExpandAliases(self, words):
    # type: (List[word__CompoundWord]) -> Optional[command_t]
    """Try to expand aliases.

    Args:
      words: A list of CompoundWord

    Returns:
      A new LST node, or None.

    Our implementation of alias has two design choices:
    - Where to insert it in parsing.  We do it at the end of ParseSimpleCommand.
    - What grammar rule to parse the expanded alias buffer with.  In our case
      it's ParseCommand().

    This doesn't quite match what other shells do, but I can't figure out a
    better places.

    Most test cases pass, except for ones like:

    alias LBRACE='{'
    LBRACE echo one; echo two; }

    alias MULTILINE='echo 1
    echo 2
    echo 3'
    MULTILINE

    NOTE: dash handles aliases in a totally diferrent way.  It has a global
    variable checkkwd in parser.c.  It assigns it all over the grammar, like
    this:

    checkkwd = CHKNL | CHKKWD | CHKALIAS;

    The readtoken() function checks (checkkwd & CHKALIAS) and then calls
    lookupalias().  This seems to provide a consistent behavior among shells,
    but it's less modular and testable.

    Bash also uses a global 'parser_state & PST_ALEXPNEXT'.

    Returns:
      A command node if any aliases were expanded, or None otherwise.
    """
    # Start a new list if there aren't any.  This will be passed recursively
    # through CommandParser instances.
    aliases_in_flight = self.aliases_in_flight or []

    first_word_str = None  # for error message
    argv0_spid = word.LeftMostSpanForWord(words[0])

    expanded = []
    i = 0
    n = len(words)

    while i < n:
      w = words[i]

      ok, word_str, quoted = word.StaticEval(w)
      if not ok or quoted:
        break

      alias_exp = self.aliases.get(word_str)
      if alias_exp is None:
        break

      # Prevent infinite loops.  This is subtle: we want to prevent infinite
      # expansion of alias echo='echo x'.  But we don't want to prevent
      # expansion of the second word in 'echo echo', so we add 'i' to
      # "aliases_in_flight".
      if (word_str, i) in aliases_in_flight:
        break

      if i == 0:
        first_word_str = word_str  # for error message

      #log('%r -> %r', word_str, alias_exp)
      aliases_in_flight.append((word_str, i))
      expanded.append(alias_exp)
      i += 1

      if not alias_exp.endswith(' '):
        # alias e='echo [ ' is the same expansion as
        # alias e='echo ['
        # The trailing space indicates whether we should continue to expand
        # aliases; it's not part of it.
        expanded.append(' ')
        break  # No more expansions

    if not expanded:  # No expansions; caller does parsing.
      return None

    # We got some expansion.  Now copy the rest of the words.

    # We need each NON-REDIRECT word separately!  For example:
    # $ echo one >out two
    # dash/mksh/zsh go beyond the first redirect!
    while i < n:
      w = words[i]
      spid1 = word.LeftMostSpanForWord(w)
      spid2 = word.RightMostSpanForWord(w)

      span1 = self.arena.GetLineSpan(spid1)
      span2 = self.arena.GetLineSpan(spid2)

      if 0:
        log('spid1 = %d, spid2 = %d', spid1, spid2)
        n1 = self.arena.GetLineNumber(span1.line_id)
        n2 = self.arena.GetLineNumber(span2.line_id)
        log('span1 %s line %d %r', span1, n1, self.arena.GetLine(span1.line_id))
        log('span2 %s line %d %r', span2, n2, self.arena.GetLine(span2.line_id))

      if span1.line_id == span2.line_id:
        line = self.arena.GetLine(span1.line_id)
        piece = line[span1.col : span2.col + span2.length]
        expanded.append(piece)
      else:
        # NOTE: The xrange(left_spid, right_spid) algorithm won't work for
        # commands like this:
        #
        # myalias foo`echo hi`bar
        #
        # That is why we only support words over 1 or 2 lines.

        raise NotImplementedError(
            'line IDs %d != %d' % (span1.line_id, span2.line_id))

      expanded.append(' ')  # Put space back between words.
      i += 1

    code_str = ''.join(expanded)
    lines = code_str.splitlines(True)  # Keep newlines

    # NOTE: self.arena isn't correct here.  Breaks line invariant.
    line_reader = reader.StringLineReader(code_str, self.arena)
    cp = self.parse_ctx.MakeOshParser(line_reader, emit_comp_dummy=True,
                                      aliases_in_flight=aliases_in_flight)

    # The interaction between COMPLETION and ALIASES requires special care.
    # See docstring of BeginAliasExpansion() in parse_lib.py.

    extent = None  # TODO: GetLineNumber / GetLineSource for current span_id?
    self.arena.PushSource(source.Alias(first_word_str, argv0_spid))
    trail = self.parse_ctx.trail
    trail.BeginAliasExpansion()
    try:
      # _ParseCommandTerm() handles multiline commands, compound commands, etc.
      # as opposed to ParseLogicalLine()
      node = cp._ParseCommandTerm()
    except util.ParseError as e:
      # Failure to parse alias expansion is a fatal error
      # We don't need more handling here/
      raise
    finally:
      trail.EndAliasExpansion()
      self.arena.PopSource()

    if 0:
      log('AFTER expansion:')
      node.PrettyPrint()

    return node

  # Flags that indicate an assignment should be parsed like a command.
  _ASSIGN_COMMANDS = set([
      (Id.Assign_Declare, '-f'),  # function defs
      (Id.Assign_Declare, '-F'),  # function names
      (Id.Assign_Declare, '-p'),  # print

      (Id.Assign_Typeset, '-f'),
      (Id.Assign_Typeset, '-F'),
      (Id.Assign_Typeset, '-p'),

      (Id.Assign_Local, '-p'),
      (Id.Assign_Readonly, '-p'),
      # Hm 'export -p' is more like a command.  But we're parsing it
      # dynamically now because of some wrappers.
      # Maybe we could change this.
      #(Id.Assign_Export, '-p'),
  ])
  # Flags to parse like assignments: -a -r -x (and maybe -i)

  def ParseSimpleCommand(self):
    # type: () -> command_t
    """
    Fixed transcription of the POSIX grammar (TODO: port to grammar/Shell.g)

    io_file        : '<'       filename
                   | LESSAND   filename
                     ...

    io_here        : DLESS     here_end
                   | DLESSDASH here_end

    redirect       : IO_NUMBER (io_redirect | io_here)

    prefix_part    : ASSIGNMENT_WORD | redirect
    cmd_part       : WORD | redirect

    assign_kw      : Declare | Export | Local | Readonly

    # Without any words it is parsed as a command, not an assigment
    assign_listing : assign_kw

    # Now we have something to do (might be changing assignment flags too)
    # NOTE: any prefixes should be a warning, but they are allowed in shell.
    assignment     : prefix_part* assign_kw (WORD | ASSIGNMENT_WORD)+

    # an external command, a function call, or a builtin -- a "word_command"
    word_command   : prefix_part* cmd_part+

    simple_command : assign_listing
                   | assignment
                   | proc_command

    Simple imperative algorithm:

    1) Read a list of words and redirects.  Append them to separate lists.
    2) Look for the first non-assignment word.  If it's declare, etc., then
    keep parsing words AND assign words.  Otherwise, just parse words.
    3) If there are no non-assignment words, then it's a global assignment.

    { redirects, global assignments } OR
    { redirects, prefix_bindings, words } OR
    { redirects, ERROR_prefix_bindings, keyword, assignments, words }

    THEN CHECK that prefix bindings don't have any array literal parts!
    global assignment and keyword assignments can have the of course.
    well actually EXPORT shouldn't have them either -- WARNING

    3 cases we want to warn: prefix_bindings for assignment, and array literal
    in prefix bindings, or export

    A command can be an assignment word, word, or redirect on its own.

        ls
        >out.txt

        >out.txt FOO=bar   # this touches the file, and hten

    Or any sequence:
        ls foo bar
        <in.txt ls foo bar >out.txt
        <in.txt ls >out.txt foo bar

    Or add one or more environment bindings:
        VAR=val env
        >out.txt VAR=val env

    here_end vs filename is a matter of whether we test that it's quoted.  e.g.
    <<EOF vs <<'EOF'.
    """
    result = self._ScanSimpleCommand()
    redirects, words = result

    if not words:  # e.g.  >out.txt  # redirect without words
      node = command.SimpleCommand(None, redirects, None)  # type: command_t
      return node

    preparsed_list, suffix_words = _SplitSimpleCommandPrefix(words)

    # Set a reference to words and redirects for completion.  We want to
    # inspect this state after a failed parse.
    self.parse_ctx.trail.SetLatestWords(suffix_words, redirects)

    if not suffix_words:  # ONE=1 a[x]=1 TWO=2  (with no other words)
      if redirects:
        left_token, _, _, _ = preparsed_list[0]
        p_die("Global assignment shouldn't have redirects", token=left_token)

      pairs = []
      for preparsed in preparsed_list:
        pairs.append(_MakeAssignPair(self.parse_ctx, preparsed, self.arena))

      node = command.Assignment(Id.Assign_None, [], pairs)
      left_spid = word.LeftMostSpanForWord(words[0])
      node.spids.append(left_spid)  # no keyword spid to skip past
      return node

    kind, kw_token = word.KeywordToken(suffix_words[0])

    if kind == Kind.Assign:
      # Here we StaticEval suffix_words[1] to see if we have an ASSIGNMENT COMMAND
      # like 'typeset -p', which lists variables -- a SimpleCommand rather than
      # an Assignment.
      #
      # Note we're not handling duplicate flags like 'typeset -pf'.  I see this
      # in bashdb (bash debugger) but it can just be changed to 'typeset -p
      # -f'.
      is_command = False
      if len(suffix_words) > 1:
        ok, val, _ = word.StaticEval(suffix_words[1])
        if ok and (kw_token.id, val) in self._ASSIGN_COMMANDS:
          is_command = True

      if is_command:  # declare -f, declare -p, typeset -p, etc.
        node = _MakeSimpleCommand(preparsed_list, suffix_words, redirects)
        return node

      if redirects:
        # Attach the error location to the keyword.  It would be more precise
        # to attach it to the
        p_die("Assignments shouldn't have redirects", token=kw_token)

      if preparsed_list:  # FOO=bar local spam=eggs not allowed
        # Use the location of the first value.  TODO: Use the whole word
        # before splitting.
        left_token, _, _, _ = preparsed_list[0]
        p_die("Assignments shouldn't have environment bindings", token=left_token)

      # declare str='', declare -a array=()
      node = _MakeAssignment(self.parse_ctx, kw_token.id, suffix_words)
      node.spids.append(kw_token.span_id)
      return node

    if kind == Kind.ControlFlow:
      if redirects:
        p_die("Control flow shouldn't have redirects", token=kw_token)

      if preparsed_list:  # FOO=bar local spam=eggs not allowed
        # TODO: Change location as above
        left_token, _, _, _ = preparsed_list[0]
        p_die("Control flow shouldn't have environment bindings",
              token=left_token)

      # Attach the token for errors.  (Assignment may not need it.)
      if len(suffix_words) == 1:
        arg_word = None
      elif len(suffix_words) == 2:
        arg_word = suffix_words[1]
      else:
        p_die('Unexpected argument to %r', kw_token.val, word=suffix_words[2])

      return command.ControlFlow(kw_token, arg_word)

    # If any expansions were detected, then parse again.
    expanded_node = self._MaybeExpandAliases(suffix_words)
    if expanded_node:
      # Attach env bindings and redirects to the expanded node.
      more_env = []  # type: List[env_pair]
      _AppendMoreEnv(preparsed_list, more_env)
      node = command.ExpandedAlias(expanded_node, redirects, more_env)
      return node

    # TODO check that we don't have env1=x x[1]=y env2=z here.

    # FOO=bar printenv.py FOO
    node = _MakeSimpleCommand(preparsed_list, suffix_words, redirects)
    return node

  def ParseBraceGroup(self):
    # type: () -> command__BraceGroup
    """
    brace_group      : LBrace command_list RBrace ;
    """
    left_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.Lit_LBrace)

    c_list = self._ParseCommandList()
    assert c_list is not None

    # Not needed
    #right_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.Lit_RBrace)

    node = command.BraceGroup(c_list.children)
    node.spids.append(left_spid)
    return node

  def ParseDoGroup(self):
    # type: () -> command__DoGroup
    """
    Used by ForEach, ForExpr, While, Until.  Should this be a Do node?

    do_group         : Do command_list Done ;          /* Apply rule 6 */
    """
    self._Eat(Id.KW_Do)
    do_spid = word.LeftMostSpanForWord(self.cur_word)  # after _Eat

    c_list = self._ParseCommandList()  # could be any thing
    assert c_list is not None

    self._Eat(Id.KW_Done)
    done_spid = word.LeftMostSpanForWord(self.cur_word)  # after _Eat

    node = command.DoGroup(c_list.children)
    node.spids.extend((do_spid, done_spid))
    return node

  def ParseForWords(self):
    # type: () -> Tuple[List[word__CompoundWord], int]
    """
    for_words        : WORD* for_sep
                     ;
    for_sep          : ';' newline_ok
                     | NEWLINES
                     ;
    """
    words = []
    # The span_id of any semi-colon, so we can remove it.
    semi_spid = const.NO_INTEGER

    while True:
      self._Peek()
      if self.c_id == Id.Op_Semi:
        w = cast(word__TokenWord, self.cur_word)
        semi_spid = w.token.span_id
        self._Next()
        self._NewlineOk()
        break
      elif self.c_id == Id.Op_Newline:
        self._Next()
        break

      if not isinstance(self.cur_word, word__CompoundWord):
        # TODO: Can we also show a pointer to the 'for' keyword?
        p_die('Invalid word in for loop', word=self.cur_word)

      words.append(self.cur_word)
      self._Next()
    return words, semi_spid

  def _ParseForExprLoop(self):
    # type: () -> command__ForExpr
    """
    for (( init; cond; update )) for_sep? do_group
    """
    node = self.w_parser.ReadForExpression()
    self._Next()

    self._Peek()
    if self.c_id == Id.Op_Semi:
      self._Next()
      self._NewlineOk()
    elif self.c_id == Id.Op_Newline:
      self._Next()
    elif self.c_id == Id.KW_Do:  # missing semicolon/newline allowed
      pass
    else:
      p_die('Invalid word after for expression', word=self.cur_word)

    node.body = self.ParseDoGroup()
    return node

  def _ParseForEachLoop(self, for_spid):
    # type: (int) -> command__ForEach
    node = command.ForEach()
    node.do_arg_iter = False
    node.spids.append(for_spid)  # for $LINENO and error fallback

    ok, iter_name, quoted = word.StaticEval(self.cur_word)
    if not ok or quoted:
      p_die("Loop variable name should be a constant", word=self.cur_word)
    if not match.IsValidVarName(iter_name):
      p_die("Invalid loop variable name", word=self.cur_word)
    node.iter_name = iter_name
    self._Next()  # skip past name

    self._NewlineOk()

    in_spid = const.NO_INTEGER
    semi_spid = const.NO_INTEGER

    self._Peek()
    if self.c_id == Id.KW_In:
      self._Next()  # skip in

      in_spid = word.LeftMostSpanForWord(self.cur_word) + 1
      iter_words, semi_spid = self.ParseForWords()

      words2 = braces.BraceDetectAll(iter_words)
      words3 = word.TildeDetectAll(words2)
      node.iter_words = words3

    elif self.c_id == Id.Op_Semi:  # for x; do
      node.do_arg_iter = True  # implicit for loop
      self._Next()

    elif self.c_id == Id.KW_Do:
      node.do_arg_iter = True  # implicit for loop
      # do not advance

    else:  # for foo BAD
      p_die('Unexpected word after for loop variable', word=self.cur_word)

    node.body = self.ParseDoGroup()

    node.spids.append(in_spid)
    node.spids.append(semi_spid)
    return node

  def ParseFor(self):
    # type: () -> command_t
    """
    for_clause : For for_name newline_ok (in for_words? for_sep)? do_group ;
               | For '((' ... TODO
    """
    for_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.KW_For)

    self._Peek()
    if self.c_id == Id.Op_DLeftParen:
      node = self._ParseForExprLoop()  # type: command_t
    else:
      node = self._ParseForEachLoop(for_spid)

    return node

  def ParseWhileUntil(self):
    # type: () -> command__WhileUntil
    """
    while_clause     : While command_list do_group ;
    until_clause     : Until command_list do_group ;
    """
    # This is ensured by the WordParser.  Keywords are returned in a CompoundWord.
    # It's not a TokenWord because we could have something like 'whileZZ true'.
    assert isinstance(self.cur_word, word__CompoundWord)  # for MyPy
    assert isinstance(self.cur_word.parts[0], word_part__LiteralPart)  # for MyPy

    keyword = self.cur_word.parts[0].token
    # This is ensured by the caller
    assert keyword.id in (Id.KW_While, Id.KW_Until), keyword
    self._Next()  # skip while

    cond_node = self._ParseCommandList()
    assert cond_node is not None

    body_node = self.ParseDoGroup()
    assert body_node is not None

    return command.WhileUntil(keyword, cond_node.children, body_node)

  def ParseCaseItem(self):
    # type: () -> case_arm
    """
    case_item: '('? pattern ('|' pattern)* ')'
               newline_ok command_term? trailer? ;
    """
    self.lexer.PushHint(Id.Op_RParen, Id.Right_CasePat)

    left_spid = word.LeftMostSpanForWord(self.cur_word)
    if self.c_id == Id.Op_LParen:
      self._Next()

    pat_words = []
    while True:
      self._Peek()
      pat_words.append(self.cur_word)
      self._Next()

      self._Peek()
      if self.c_id == Id.Op_Pipe:
        self._Next()
      else:
        break

    rparen_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.Right_CasePat)
    self._NewlineOk()

    if self.c_id not in (Id.Op_DSemi, Id.KW_Esac):
      c_list = self._ParseCommandTerm()
      action_children = c_list.children
    else:
      action_children = []

    dsemi_spid = const.NO_INTEGER
    last_spid = const.NO_INTEGER
    self._Peek()
    if self.c_id == Id.KW_Esac:
      last_spid = word.LeftMostSpanForWord(self.cur_word)
    elif self.c_id == Id.Op_DSemi:
      dsemi_spid = word.LeftMostSpanForWord(self.cur_word)
      self._Next()
    else:
      # Happens on EOF
      p_die('Expected ;; or esac', word=self.cur_word)

    self._NewlineOk()

    arm = syntax_asdl.case_arm(pat_words, action_children)
    arm.spids.extend((left_spid, rparen_spid, dsemi_spid, last_spid))
    return arm

  def ParseCaseList(self, arms):
    # type: (List[case_arm]) -> None
    """
    case_list: case_item (DSEMI newline_ok case_item)* DSEMI? newline_ok;
    """
    self._Peek()

    while True:
      # case item begins with a command word or (
      if self.c_id == Id.KW_Esac:
        break
      if self.c_kind != Kind.Word and self.c_id != Id.Op_LParen:
        break
      arm = self.ParseCaseItem()

      arms.append(arm)
      self._Peek()
      # Now look for DSEMI or ESAC

  def ParseCase(self):
    # type: () -> command__Case
    """
    case_clause      : Case WORD newline_ok in newline_ok case_list? Esac ;
    """
    case_node = command.Case()

    case_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Next()  # skip case

    self._Peek()
    case_node.to_match = self.cur_word
    self._Next()

    self._NewlineOk()
    in_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.KW_In)
    self._NewlineOk()

    if self.c_id != Id.KW_Esac:  # empty case list
      self.ParseCaseList(case_node.arms)
      # TODO: should it return a list of nodes, and extend?
      self._Peek()

    esac_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.KW_Esac)
    self._Next()

    case_node.spids.extend((case_spid, in_spid, esac_spid))
    return case_node

  def _ParseElifElse(self, if_node):
    # type: (command__If) -> None
    """
    else_part: (Elif command_list Then command_list)* Else command_list ;
    """
    arms = if_node.arms

    self._Peek()
    while self.c_id == Id.KW_Elif:
      elif_spid = word.LeftMostSpanForWord(self.cur_word)

      self._Next()  # skip elif
      cond = self._ParseCommandList()
      assert cond is not None

      then_spid = word.LeftMostSpanForWord(self.cur_word)
      self._Eat(Id.KW_Then)

      body = self._ParseCommandList()
      assert body is not None

      arm = syntax_asdl.if_arm(cond.children, body.children)
      arm.spids.extend((elif_spid, then_spid))
      arms.append(arm)

    if self.c_id == Id.KW_Else:
      else_spid = word.LeftMostSpanForWord(self.cur_word)
      self._Next()
      body = self._ParseCommandList()
      assert body is not None
      if_node.else_action = body.children
    else:
      else_spid = const.NO_INTEGER

    if_node.spids.append(else_spid)

  def ParseIf(self):
    # type: () -> command__If
    """
    if_clause        : If command_list Then command_list else_part? Fi ;
    """
    if_node = command.If()
    self._Next()  # skip if

    cond = self._ParseCommandList()
    assert cond is not None

    then_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.KW_Then)

    body = self._ParseCommandList()
    assert body is not None

    arm = syntax_asdl.if_arm(cond.children, body.children)
    arm.spids.extend((const.NO_INTEGER, then_spid))  # no if spid at first?
    if_node.arms.append(arm)

    if self.c_id in (Id.KW_Elif, Id.KW_Else):
      self._ParseElifElse(if_node)
    else:
      if_node.spids.append(const.NO_INTEGER)  # no else spid

    fi_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.KW_Fi)

    if_node.spids.append(fi_spid)
    return if_node

  def ParseTime(self):
    # type: () -> command_t
    """
    time [-p] pipeline

    According to bash help.
    """
    self._Next()  # skip time
    pipeline = self.ParsePipeline()
    return command.TimeBlock(pipeline)

  def ParseCompoundCommand(self):
    # type: () -> command_t
    """
    compound_command : brace_group
                     | subshell
                     | for_clause
                     | while_clause
                     | until_clause
                     | if_clause
                     | case_clause
                     | time_clause
                     | [[ BoolExpr ]]
                     | (( ArithExpr ))
                     ;
    """
    if self.c_id == Id.Lit_LBrace:
      return self.ParseBraceGroup()
    if self.c_id == Id.Op_LParen:
      return self.ParseSubshell()

    if self.c_id == Id.KW_For:
      return self.ParseFor()
    if self.c_id in (Id.KW_While, Id.KW_Until):
      return self.ParseWhileUntil()

    if self.c_id == Id.KW_If:
      return self.ParseIf()
    if self.c_id == Id.KW_Case:
      return self.ParseCase()
    if self.c_id == Id.KW_Time:
      return self.ParseTime()

    # Example of redirect that is observable:
    # $ (( $(echo one 1>&2; echo 2) > 0 )) 2> out.txt
    if self.c_id == Id.KW_DLeftBracket:
      return self.ParseDBracket()

    if self.c_id == Id.Op_DLeftParen:
      return self.ParseDParen()

    if self.c_id == Id.KW_Var:
      kw_token = word.LiteralToken(self.cur_word)
      self._Next()
      return self.w_parser.ParseVar(kw_token)

    if self.c_id == Id.KW_SetVar:
      kw_token = word.LiteralToken(self.cur_word)
      self._Next()
      return self.w_parser.ParseSetVar(kw_token)

    # This never happens?
    p_die('Unexpected word while parsing compound command', word=self.cur_word)

  def ParseFunctionBody(self, func):
    # type: (command__FuncDef) -> None
    """
    function_body    : compound_command io_redirect* ; /* Apply rule 9 */
    """
    func.body = self.ParseCompoundCommand()
    func.redirects = self._ParseRedirectList()

  def ParseFunctionDef(self):
    # type: () -> command__FuncDef
    """
    function_header : fname '(' ')'
    function_def     : function_header newline_ok function_body ;

    Precondition: Looking at the function name.
    Post condition:

    NOTE: There is an ambiguity with:

    function foo ( echo hi ) and
    function foo () ( echo hi )

    Bash only accepts the latter, though it doesn't really follow a grammar.
    """
    left_spid = word.LeftMostSpanForWord(self.cur_word)

    # for MyPy, caller ensures
    assert isinstance(self.cur_word, word__CompoundWord)
    ok, name = word.AsFuncName(self.cur_word)
    if not ok:
      p_die('Invalid function name', word=self.cur_word)

    self._Next()  # skip function name

    # Must be true beacuse of lookahead
    self._Peek()
    assert self.c_id == Id.Op_LParen, self.cur_word

    self.lexer.PushHint(Id.Op_RParen, Id.Right_FuncDef)
    self._Next()

    self._Eat(Id.Right_FuncDef)
    after_name_spid = word.LeftMostSpanForWord(self.cur_word) + 1

    self._NewlineOk()

    func = command.FuncDef()
    func.name = name

    self.ParseFunctionBody(func)

    func.spids.append(left_spid)
    func.spids.append(after_name_spid)
    return func

  def ParseKshFunctionDef(self):
    # type: () -> command__FuncDef
    """
    ksh_function_def : 'function' fname ( '(' ')' )? newline_ok function_body
    """
    left_spid = word.LeftMostSpanForWord(self.cur_word)

    self._Next()  # skip past 'function'

    # for MyPy, caller ensures
    assert isinstance(self.cur_word, word__CompoundWord)
    self._Peek()
    ok, name = word.AsFuncName(self.cur_word)
    if not ok:
      p_die('Invalid KSH-style function name', word=self.cur_word)

    after_name_spid = word.LeftMostSpanForWord(self.cur_word) + 1
    self._Next()  # skip past 'function name

    self._Peek()
    if self.c_id == Id.Op_LParen:
      self.lexer.PushHint(Id.Op_RParen, Id.Right_FuncDef)
      self._Next()
      self._Eat(Id.Right_FuncDef)
      # Change it: after )
      after_name_spid = word.LeftMostSpanForWord(self.cur_word) + 1

    self._NewlineOk()

    func = command.FuncDef()
    func.name = name

    self.ParseFunctionBody(func)

    func.spids.append(left_spid)
    func.spids.append(after_name_spid)
    return func

  def ParseCoproc(self):
    # type: () -> command_t
    """
    TODO: command__Coproc?
    """
    raise NotImplementedError

  def ParseSubshell(self):
    # type: () -> command__Subshell
    left_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Next()  # skip past (

    # Ensure that something $( (cd / && pwd) ) works.  If ) is already on the
    # translation stack, we want to delay it.

    self.lexer.PushHint(Id.Op_RParen, Id.Right_Subshell)

    c_list = self._ParseCommandList()
    node = command.Subshell(c_list)

    right_spid = word.LeftMostSpanForWord(self.cur_word)
    self._Eat(Id.Right_Subshell)

    node.spids.extend((left_spid, right_spid))
    return node

  def ParseDBracket(self):
    # type: () -> command__DBracket
    """
    Pass the underlying word parser off to the boolean expression parser.
    """
    maybe_error_word = self.cur_word
    left_spid = word.LeftMostSpanForWord(self.cur_word)
    # TODO: Test interactive.  Without closing ]], you should get > prompt
    # (PS2)

    self._Next()  # skip [[
    b_parser = bool_parse.BoolParser(self.w_parser)
    bnode = b_parser.Parse()  # May raise
    right_spid = word.LeftMostSpanForWord(self.cur_word)

    node = command.DBracket(bnode)
    node.spids.append(left_spid)
    node.spids.append(right_spid)
    return node

  def ParseDParen(self):
    # type: () -> command__DParen
    maybe_error_word = self.cur_word
    left_spid = word.LeftMostSpanForWord(self.cur_word)

    self._Next()  # skip ((
    anode, right_spid = self.w_parser.ReadDParen()
    assert anode is not None

    node = command.DParen(anode)
    node.spids.append(left_spid)
    node.spids.append(right_spid)
    return node

  def ParseCommand(self):
    # type: () -> command_t
    """
    command          : simple_command
                     | compound_command io_redirect*
                     | function_def
                     | ksh_function_def
                     ;
    """
    self._Peek()

    if self.c_id in NOT_FIRST_WORDS:
      p_die('Unexpected word when parsing command', word=self.cur_word)

    if self.c_id == Id.KW_Function:
      return self.ParseKshFunctionDef()

    # TODO: We should have another Kind for "initial keywords".  And then
    # NOT_FIRST_WORDS are "secondary keywords".
    if self.c_id in (
        Id.KW_DLeftBracket, Id.Op_DLeftParen, Id.Op_LParen, Id.Lit_LBrace,
        Id.KW_For, Id.KW_While, Id.KW_Until, Id.KW_If, Id.KW_Case, Id.KW_Time,
        Id.KW_Var, Id.KW_SetVar):
      node = self.ParseCompoundCommand()

      # NOTE: this is unsafe within the type system because redirects aren't on
      # the base class.
      if node.tag not in (command_e.TimeBlock, command_e.OilAssign):
        node.redirects = self._ParseRedirectList()  # type: ignore
      return node

    # NOTE: I added this to fix cases in parse-errors.test.sh, but it doesn't
    # work because Lit_RBrace is in END_LIST below.

    # TODO: KW_Do is also invalid here.
    if self.c_id == Id.Lit_RBrace:
      p_die('Unexpected right brace', word=self.cur_word)

    if self.c_kind == Kind.Redir:  # Leading redirect
      return self.ParseSimpleCommand()

    if self.c_kind == Kind.Word:
      # NOTE: At the top level, only TokenWord and CompoundWord are possible.
      # Can this be modelled better in the type system, removing asserts?
      assert isinstance(self.cur_word, word__CompoundWord)
      if (self.w_parser.LookAhead() == Id.Op_LParen and
          not word.IsVarLike(self.cur_word)):
          return self.ParseFunctionDef()  # f() { echo; }  # function
      # echo foo
      # f=(a b c)  # array
      # array[1+2]+=1
      return self.ParseSimpleCommand()

    if self.c_kind == Kind.Eof:
      p_die("Unexpected EOF while parsing command", word=self.cur_word)

    # NOTE: This only happens in batch mode in the second turn of the loop!
    # e.g. )
    p_die("Invalid word while parsing command", word=self.cur_word)

  def ParsePipeline(self):
    # type: () -> command_t
    """
    pipeline         : Bang? command ( '|' newline_ok command )* ;
    """
    negated = False

    # For blaming failures
    pipeline_spid = const.NO_INTEGER

    self._Peek()
    if self.c_id == Id.KW_Bang:
      pipeline_spid = word.LeftMostSpanForWord(self.cur_word)
      negated = True
      self._Next()

    child = self.ParseCommand()
    assert child is not None

    children = [child]

    self._Peek()
    if self.c_id not in (Id.Op_Pipe, Id.Op_PipeAmp):
      if negated:
        node = command.Pipeline(children, negated)
        node.spids.append(pipeline_spid)
        return node
      else:
        return child

    pipe_index = 0
    stderr_indices = []

    if self.c_id == Id.Op_PipeAmp:
      stderr_indices.append(pipe_index)
    pipe_index += 1

    while True:
      # Set it to the first | if it isn't already set.
      if pipeline_spid == const.NO_INTEGER:
        pipeline_spid = word.LeftMostSpanForWord(self.cur_word)

      self._Next()  # skip past Id.Op_Pipe or Id.Op_PipeAmp
      self._NewlineOk()

      child = self.ParseCommand()
      children.append(child)

      self._Peek()
      if self.c_id not in (Id.Op_Pipe, Id.Op_PipeAmp):
        break

      if self.c_id == Id.Op_PipeAmp:
        stderr_indices.append(pipe_index)
      pipe_index += 1

    node = command.Pipeline(children, negated, stderr_indices)
    node.spids.append(pipeline_spid)
    return node

  def ParseAndOr(self):
    # type: () -> command_t
    """
    and_or           : and_or ( AND_IF | OR_IF ) newline_ok pipeline
                     | pipeline

    Note that it is left recursive and left associative.  We parse it
    iteratively with a token of lookahead.
    """
    child = self.ParsePipeline()
    assert child is not None

    self._Peek()
    if self.c_id not in (Id.Op_DPipe, Id.Op_DAmp):
      return child

    ops = []
    children = [child]

    while True:
      ops.append(self.c_id)

      self._Next()  # skip past || &&
      self._NewlineOk()

      child = self.ParsePipeline()

      children.append(child)

      self._Peek()
      if self.c_id not in (Id.Op_DPipe, Id.Op_DAmp):
        break

    node = command.AndOr(ops, children)
    return node

  # NOTE: _ParseCommandLine and _ParseCommandTerm are similar, but different.

  # At the top level, We want to execute after every line:
  # - to process alias
  # - to process 'exit', because invalid syntax might appear after it

  # But for say a while loop body, we want to parse the whole thing at once, and
  # then execute it.  We don't want to parse it over and over again!

  # COMPARE
  # command_line     : and_or (sync_op and_or)* trailer? ;   # TOP LEVEL
  # command_term     : and_or (trailer and_or)* ;            # CHILDREN

  def _ParseCommandLine(self):
    # type: () -> command_t
    """
    command_line     : and_or (sync_op and_or)* trailer? ;
    trailer          : sync_op newline_ok
                     | NEWLINES;
    sync_op          : '&' | ';';

    NOTE: This rule causes LL(k > 1) behavior.  We would have to peek to see if
    there is another command word after the sync op.

    But it's easier to express imperatively.  Do the following in a loop:
    1. ParseAndOr
    2. Peek.
       a. If there's a newline, then return.  (We're only parsing a single
          line.)
       b. If there's a sync_op, process it.  Then look for a newline and
          return.  Otherwise, parse another AndOr.
    """
    # This END_LIST is slightly different than END_LIST in _ParseCommandTerm.
    # I don't think we should add anything else here; otherwise it will be
    # ignored at the end of ParseInteractiveLine(), e.g. leading to bug #301.
    END_LIST = (Id.Op_Newline, Id.Eof_Real)

    children = []
    done = False
    while not done:
      child = self.ParseAndOr()

      self._Peek()
      if self.c_id in (Id.Op_Semi, Id.Op_Amp):  # also Id.Op_Amp.
        w = cast(word__TokenWord, self.cur_word)  # for MyPy
        child = command.Sentence(child, w.token)
        self._Next()

        self._Peek()
        if self.c_id in END_LIST:
          done = True

      elif self.c_id in END_LIST:
        done = True

      else:
        # e.g. echo a(b)
        p_die('Unexpected word while parsing command line',
              word=self.cur_word)

      children.append(child)

    # Simplify the AST.
    if len(children) > 1:
      return command.CommandList(children)
    else:
      return children[0]

  def _ParseCommandTerm(self):
    # type: () -> command__CommandList
    """"
    command_term     : and_or (trailer and_or)* ;
    trailer          : sync_op newline_ok
                     | NEWLINES;
    sync_op          : '&' | ';';

    This is handled in imperative style, like _ParseCommandLine.
    Called by _ParseCommandList for all blocks, and also for ParseCaseItem,
    which is slightly different.  (HOW?  Is it the DSEMI?)

    Returns:
      syntax_asdl.command
    """
    # Token types that will end the command term.
    END_LIST = (self.eof_id, Id.Right_Subshell, Id.Lit_RBrace, Id.Op_DSemi)

    # NOTE: This is similar to _ParseCommandLine.
    #
    # - Why aren't we doing END_LIST in _ParseCommandLine?
    #   - Because you will never be inside $() at the top level.
    #   - We also know it will end in a newline.  It can't end in "fi"!
    #   - example: if true; then { echo hi; } fi

    children = []
    done = False
    while not done:
      self._Peek()

      # Most keywords are valid "first words".  But do/done/then do not BEGIN
      # commands, so they are not valid.
      if self.c_id in NOT_FIRST_WORDS:
        break

      child = self.ParseAndOr()

      self._Peek()
      if self.c_id == Id.Op_Newline:
        self._Next()

        self._Peek()
        if self.c_id in END_LIST:
          done = True

      elif self.c_id in (Id.Op_Semi, Id.Op_Amp):
        w = cast(word__TokenWord, self.cur_word)  # for MyPy
        child = command.Sentence(child, w.token)
        self._Next()

        self._Peek()
        if self.c_id == Id.Op_Newline:
          self._Next()  # skip over newline

          # Test if we should keep going.  There might be another command after
          # the semi and newline.
          self._Peek()
          if self.c_id in END_LIST:  # \n EOF
            done = True

        elif self.c_id in END_LIST:  # ; EOF
          done = True

      elif self.c_id in END_LIST:  # EOF
        done = True

      else:
        pass  # e.g. "} done", "fi fi", ") fi", etc. is OK

      children.append(child)

    self._Peek()

    return command.CommandList(children)

  # TODO: Make this private.
  def _ParseCommandList(self):
    # type: () -> command__CommandList
    """
    command_list     : newline_ok command_term trailer? ;

    This one is called by all the compound commands.  It's basically a command
    block.

    NOTE: Rather than translating the CFG directly, the code follows a style
    more like this: more like this: (and_or trailer)+.  It makes capture
    easier.
    """
    self._NewlineOk()
    node = self._ParseCommandTerm()
    return node

  def ParseLogicalLine(self):
    # type: () -> command_t
    """Parse a single line for main_loop.

    A wrapper around _ParseCommandLine().  Similar but not identical to
    _ParseCommandList() and ParseCommandSub().

    Raises:
      ParseError
    """
    self._NewlineOk()
    self._Peek()
    if self.c_id == Id.Eof_Real:
      return None  # main loop checks for here docs
    node = self._ParseCommandLine()
    return node

  def ParseInteractiveLine(self):
    # type: () -> parse_result_t
    """Parse a single line for Interactive main_loop.

    Different from ParseLogicalLine because newlines are handled differently.

    Raises:
      ParseError
    """
    self._Peek()
    if self.c_id == Id.Op_Newline:
      return parse_result.EmptyLine()
    if self.c_id == Id.Eof_Real:
      return parse_result.Eof()

    node = self._ParseCommandLine()
    return parse_result.Node(node)

  def ParseCommandSub(self):
    # type: () -> command_t
    """Parse $(echo hi) and `echo hi` for word_parse.py.

    They can have multiple lines, like this:
    echo $(
      echo one
      echo two
    )
    """
    self._NewlineOk()

    if self.c_kind == Kind.Eof:  # e.g. $()
      return command.NoOp()

    # This calls ParseAndOr(), but I think it should be a loop that calls
    # _ParseCommandLine(), like oil.InteractiveLoop.
    node = self._ParseCommandTerm()
    return node

  def CheckForPendingHereDocs(self):
    # type: () -> None
    # NOTE: This happens when there is no newline at the end of a file, like
    # osh -c 'cat <<EOF'
    if self.pending_here_docs:
      node = self.pending_here_docs[0]  # Just show the first one?
      p_die('Unterminated here doc began here', word=node.here_begin)
