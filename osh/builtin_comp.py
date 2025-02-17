"""
builtin_comp.py - Completion builtins
"""

from _devbuild.gen import osh_help  # generated file
from _devbuild.gen.runtime_asdl import value_e
from core import completion
from core import ui
from core import util
#from core.util import log
from frontend import args
from frontend import lex
from osh import builtin
from osh import state


def _DefineFlags(spec):
  spec.ShortFlag('-F', args.Str, help='Complete with this function')
  spec.ShortFlag('-W', args.Str, help='Complete with these words')
  spec.ShortFlag('-P', args.Str,
      help='Prefix is added at the beginning of each possible completion after '
           'all other options have been applied.')
  spec.ShortFlag('-S', args.Str,
      help='Suffix is appended to each possible completion after '
           'all other options have been applied.')
  spec.ShortFlag('-X', args.Str,
      help='''
A glob pattern to further filter the matches.  It is applied to the list of
possible completions generated by the preceding options and arguments, and each
completion matching filterpat is removed from the list. A leading ! in
filterpat negates the pattern; in this case, any completion not matching
filterpat is removed. 
''')


def _DefineOptions(spec):
  """Common -o options for complete and compgen."""

  # bashdefault, default, filenames, nospace are used in git
  spec.Option(None, 'bashdefault',
      help='If nothing matches, perform default bash completions')
  spec.Option(None, 'default',
      help="If nothing matches, use readline's default filename completion")
  spec.Option(None, 'filenames',
      help="The completion function generates filenames and should be "
           "post-processed")
  spec.Option(None, 'dirnames',
      help="If nothing matches, perform directory name completion")
  spec.Option(None, 'nospace',
      help="Don't append a space to words completed at the end of the line")
  spec.Option(None, 'plusdirs',
      help="After processing the compspec, attempt directory name completion "
      "and return those matches.")


def _DefineActions(spec):
  """Common -A actions for complete and compgen."""

  # NOTE: git-completion.bash uses -f and -v. 
  # My ~/.bashrc on Ubuntu uses -d, -u, -j, -v, -a, -c, -b
  spec.InitActions()
  spec.Action('a', 'alias')
  spec.Action('b', 'binding')
  spec.Action('c', 'command')
  spec.Action('d', 'directory')
  spec.Action('f', 'file')
  spec.Action('j', 'job')
  spec.Action('u', 'user')
  spec.Action('v', 'variable')
  spec.Action(None, 'function')
  spec.Action(None, 'helptopic')  # help
  spec.Action(None, 'setopt')  # set -o
  spec.Action(None, 'shopt')  # shopt -s
  spec.Action(None, 'signal')  # kill -s
  spec.Action(None, 'stopped')


class _FixedWordsAction(completion.CompletionAction):
  def __init__(self, d):
    self.d = d

  def Matches(self, comp):
    for name in sorted(self.d):
      if name.startswith(comp.to_complete):
        yield name


class SpecBuilder(object):

  def __init__(self, ex, parse_ctx, word_ev, splitter, comp_lookup):
    """
    Args:
      ex: Executor for compgen -F
      parse_ctx, word_ev, splitter: for compgen -W
    """
    self.ex = ex
    self.parse_ctx = parse_ctx
    self.word_ev = word_ev
    self.splitter = splitter
    self.comp_lookup = comp_lookup

  def Build(self, argv, arg, base_opts):
    """Given flags to complete/compgen, return a UserSpec."""
    ex = self.ex

    actions = []

    # NOTE: bash doesn't actually check the name until completion time, but
    # obviously it's better to check here.
    if arg.F:
      func_name = arg.F
      func = ex.funcs.get(func_name)
      if func is None:
        raise args.UsageError('Function %r not found' % func_name)
      actions.append(completion.ShellFuncAction(ex, func, self.comp_lookup))

    # NOTE: We need completion for -A action itself!!!  bash seems to have it.
    for name in arg.actions:
      if name == 'alias':
        a = _FixedWordsAction(ex.aliases)

      elif name == 'binding':
        # TODO: Where do we get this from?
        a = _FixedWordsAction(['vi-delete'])

      elif name == 'command':
        # compgen -A command in bash is SIX things: aliases, builtins,
        # functions, keywords, external commands relative to the current
        # directory, and external commands in $PATH.

        actions.append(_FixedWordsAction(builtin.BUILTIN_NAMES))
        actions.append(_FixedWordsAction(ex.aliases))
        actions.append(_FixedWordsAction(ex.funcs))
        actions.append(_FixedWordsAction(lex.OSH_KEYWORD_NAMES))
        actions.append(completion.FileSystemAction(exec_only=True))

        # Look on the file system.
        a = completion.ExternalCommandAction(ex.mem)

      elif name == 'directory':
        a = completion.FileSystemAction(dirs_only=True)

      elif name == 'file':
        a = completion.FileSystemAction()

      elif name == 'function':
        a = _FixedWordsAction(ex.funcs)

      elif name == 'job':
        a = _FixedWordsAction(['jobs-not-implemented'])

      elif name == 'user':
        a = completion.UsersAction()

      elif name == 'variable':
        a = completion.VariablesAction(ex.mem)

      elif name == 'helptopic':
        a = _FixedWordsAction(osh_help.TOPIC_LOOKUP)

      elif name == 'setopt':
        a = _FixedWordsAction(state.SET_OPTION_NAMES)

      elif name == 'shopt':
        a = _FixedWordsAction(state.SHOPT_OPTION_NAMES)

      elif name == 'signal':
        a = _FixedWordsAction(['TODO:signals'])

      elif name == 'stopped':
        a = _FixedWordsAction(['jobs-not-implemented'])

      else:
        raise NotImplementedError(name)

      actions.append(a)

    # e.g. -W comes after -A directory
    if arg.W is not None:  # could be ''
      # NOTES:
      # - Parsing is done at REGISTRATION time, but execution and splitting is
      #   done at COMPLETION time (when the user hits tab).  So parse errors
      #   happen early.
      w_parser = self.parse_ctx.MakeWordParserForPlugin(arg.W)

      arena = self.parse_ctx.arena
      try:
        arg_word = w_parser.ReadForPlugin()
      except util.ParseError as e:
        ui.PrettyPrintError(e, arena)
        raise  # Let 'complete' or 'compgen' return 2

      a = completion.DynamicWordsAction(
          self.word_ev, self.splitter, arg_word, arena)
      actions.append(a)

    extra_actions = []
    if base_opts.get('plusdirs'):
      extra_actions.append(completion.FileSystemAction(dirs_only=True))

    # These only happen if there were zero shown.
    else_actions = []
    if base_opts.get('default'):
      else_actions.append(completion.FileSystemAction())
    if base_opts.get('dirnames'):
      else_actions.append(completion.FileSystemAction(dirs_only=True))

    if not actions and not else_actions:
      raise args.UsageError('No actions defined in completion: %s' % argv)

    p = completion.DefaultPredicate
    if arg.X:
      filter_pat = arg.X
      if filter_pat.startswith('!'):
        p = completion.GlobPredicate(False, filter_pat[1:])
      else:
        p = completion.GlobPredicate(True, filter_pat)
    return completion.UserSpec(actions, extra_actions, else_actions, p,
                               prefix=arg.P or '', suffix=arg.S or '')


# git-completion.sh uses complete -o and complete -F
COMPLETE_SPEC = args.FlagsAndOptions()

_DefineFlags(COMPLETE_SPEC)
_DefineOptions(COMPLETE_SPEC)
_DefineActions(COMPLETE_SPEC)

COMPLETE_SPEC.ShortFlag('-E',
    help='Define the compspec for an empty line')
COMPLETE_SPEC.ShortFlag('-D',
    help='Define the compspec that applies when nothing else matches')


class Complete(object):
  """complete builtin - register a completion function.

  NOTE: It's has an Executor because it creates a ShellFuncAction, which
  needs an Executor.
  """
  def __init__(self, spec_builder, comp_lookup):
    self.spec_builder = spec_builder
    self.comp_lookup = comp_lookup

  def __call__(self, arg_vec):
    argv = arg_vec.strs[1:]
    arg_r = args.Reader(argv)
    arg = COMPLETE_SPEC.Parse(arg_r)
    # TODO: process arg.opt_changes
    #log('arg %s', arg)

    commands = arg_r.Rest()

    if arg.D:
      commands.append('__fallback')  # if the command doesn't match anything
    if arg.E:
      commands.append('__first')  # empty line

    if not commands:
      self.comp_lookup.PrintSpecs()
      return 0

    base_opts = dict(arg.opt_changes)
    try:
      user_spec = self.spec_builder.Build(argv, arg, base_opts)
    except util.ParseError as e:
      # error printed above
      return 2
    for command in commands:
      self.comp_lookup.RegisterName(command, base_opts, user_spec)

    patterns = []
    for pat in patterns:
      self.comp_lookup.RegisterGlob(pat, base_opts, user_spec)

    return 0


COMPGEN_SPEC = args.FlagsAndOptions()  # for -o and -A

# TODO: Add -l for COMP_LINE.  -p for COMP_POINT ?
_DefineFlags(COMPGEN_SPEC)
_DefineOptions(COMPGEN_SPEC)
_DefineActions(COMPGEN_SPEC)


class CompGen(object):
  """Print completions on stdout."""

  def __init__(self, spec_builder):
    self.spec_builder = spec_builder

  def __call__(self, arg_vec):
    argv = arg_vec.strs[1:]
    arg_r = args.Reader(argv)
    arg = COMPGEN_SPEC.Parse(arg_r)

    if arg_r.AtEnd():
      to_complete = ''
    else:
      to_complete = arg_r.Peek()
      arg_r.Next()
      # bash allows extra arguments here.
      #if not arg_r.AtEnd():
      #  raise args.UsageError('Extra arguments')

    matched = False

    base_opts = dict(arg.opt_changes)
    try:
      user_spec = self.spec_builder.Build(argv, arg, base_opts)
    except util.ParseError as e:
      # error printed above
      return 2

    # NOTE: Matching bash in passing dummy values for COMP_WORDS and COMP_CWORD,
    # and also showing ALL COMPREPLY reuslts, not just the ones that start with
    # the word to complete.
    matched = False 
    comp = completion.Api()
    comp.Update(first='compgen', to_complete=to_complete, prev='', index=-1)
    try:
      for m, _ in user_spec.Matches(comp):
        matched = True
        print(m)
    except util.FatalRuntimeError:
      # - DynamicWordsAction: We already printed an error, so return failure.
      return 1

    # - ShellFuncAction: We do NOT get FatalRuntimeError.  We printed an error
    # in the executor, but RunFuncForCompletion swallows failures.  See test
    # case in builtin-completion.test.sh.

    # TODO:
    # - need to dedupe results.

    return 0 if matched else 1


COMPOPT_SPEC = args.FlagsAndOptions()  # for -o
_DefineOptions(COMPOPT_SPEC)


class CompOpt(object):
  """Adjust options inside user-defined completion functions."""

  def __init__(self, comp_state, errfmt):
    self.comp_state = comp_state
    self.errfmt = errfmt

  def __call__(self, arg_vec):
    argv = arg_vec.strs[1:]
    arg_r = args.Reader(argv)
    arg = COMPOPT_SPEC.Parse(arg_r)

    if not self.comp_state.currently_completing:  # bash also checks this.
      self.errfmt.Print('compopt: not currently executing a completion function')
      return 1

    self.comp_state.dynamic_opts.update(arg.opt_changes)
    #log('compopt: %s', arg)
    #log('compopt %s', base_opts)
    return 0


INIT_COMPLETION_SPEC = args.FlagsAndOptions()

INIT_COMPLETION_SPEC.ShortFlag('-n', args.Str,
    help='Do NOT split by these characters.  It omits them from COMP_WORDBREAKS.')
INIT_COMPLETION_SPEC.ShortFlag('-s',
    help='Treat --foo=bar and --foo bar the same way.')


class CompAdjust(object):
  """
  Uses COMP_ARGV and flags produce the 'words' array.  Also sets $cur, $prev,
  $cword, and $split.

  Note that we do not use COMP_WORDS, which already has splitting applied.
  bash-completion does a hack to undo or "reassemble" words after erroneous
  splitting.
  """
  def __init__(self, mem):
    self.mem = mem

  def __call__(self, arg_vec):
    argv = arg_vec.strs[1:]
    arg_r = args.Reader(argv)
    arg = INIT_COMPLETION_SPEC.Parse(arg_r)
    var_names = arg_r.Rest()  # Output variables to set
    for name in var_names:
      # Ironically we could complete these
      if name not in ['cur', 'prev', 'words', 'cword']:
        raise args.UsageError('Invalid output variable name %r' % name)
    #print(arg)

    # TODO: How does the user test a completion function programmatically?  Set
    # COMP_ARGV?
    val = self.mem.GetVar('COMP_ARGV')
    if val.tag != value_e.StrArray:
      raise args.UsageError("COMP_ARGV should be an array")
    comp_argv = val.strs

    # These are the ones from COMP_WORDBREAKS that we care about.  The rest occur
    # "outside" of words.
    break_chars = [':', '=']
    if arg.s:  # implied
      break_chars.remove('=')
    # NOTE: The syntax is -n := and not -n : -n =.
    omit_chars = arg.n or ''
    for c in omit_chars:
      if c in break_chars:
        break_chars.remove(c)

    # argv adjusted according to 'break_chars'.
    adjusted_argv = []
    for a in comp_argv:
      completion.AdjustArg(a, break_chars, adjusted_argv)

    if 'words' in var_names:
      state.SetArrayDynamic(self.mem, 'words', adjusted_argv)

    n = len(adjusted_argv)
    cur = adjusted_argv[-1]
    prev = '' if n < 2 else adjusted_argv[-2]

    if arg.s:
      if cur.startswith('--') and '=' in cur:  # Split into flag name and value
        prev, cur = cur.split('=', 1)
        split = 'true'
      else:
        split = 'false'
      # Do NOT set 'split' without -s.  Caller might not have declared it.
      # Also does not respect var_names, because we don't need it.
      state.SetStringDynamic(self.mem, 'split', split)

    if 'cur' in var_names:
      state.SetStringDynamic(self.mem, 'cur', cur)
    if 'prev' in var_names:
      state.SetStringDynamic(self.mem, 'prev', prev)
    if 'cword' in var_names:
      # Same weird invariant after adjustment
      state.SetStringDynamic(self.mem, 'cword', str(n-1))

    return 0
