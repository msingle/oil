#!/usr/bin/env bash
#
# Test set flags, sh flags.

#### $- with -c
# dash's behavior seems most sensible here?
$SH -o nounset -c 'echo $-'
## stdout: u
## OK bash stdout: huBc
## OK mksh stdout: uhc
## status: 0

#### $- with pipefail
set -o pipefail -o nounset
echo $-
## stdout: u
## status: 0
## OK bash stdout: huBs
## OK mksh stdout: ush
## N-I dash stdout-json: ""
## N-I dash status: 2

#### $- with interactive shell
$SH -c 'echo $-' | grep i || echo FALSE
$SH -i -c 'echo $-' | grep -q i && echo TRUE
## STDOUT:
FALSE
TRUE
## END

#### sh -c
$SH -c 'echo hi'
## stdout: hi
## status: 0

#### empty -c input
# had a bug here
$SH -c ''
## stdout-json: ""
## status: 0

#### empty stdin
# had a bug here
echo -n '' | $SH
## stdout-json: ""
## status: 0

#### args are passed
$SH -c 'argv.py "$@"' dummy a b
## stdout: ['a', 'b']

#### args that look like flags are passed after script
script=$TMP/sh1.sh
echo 'argv.py "$@"' > $script
chmod +x $script
$SH $script --help --help -h
## stdout: ['--help', '--help', '-h']

#### args that look like flags are passed after -c
$SH -c 'argv.py "$@"' --help --help -h
## stdout: ['--help', '-h']

#### pass short options on command line
$SH -e -c 'false; echo status=$?'
## stdout-json: ""
## status: 1

#### pass long options on command line
$SH -o errexit -c 'false; echo status=$?'
## stdout-json: ""
## status: 1

#### can continue after unknown option
# dash and mksh make this a fatal error no matter what.
set -o errexit
set -o STRICT || true # unknown option
echo hello
## stdout: hello
## status: 0
## BUG dash/mksh stdout-json: ""
## BUG dash status: 2
## BUG mksh status: 1

#### set with both options and argv
set -o errexit a b c
echo "$@"
false
echo done
## stdout: a b c
## status: 1

#### set -o vi/emacs
set -o vi
echo $?
set -o emacs
echo $?
## STDOUT:
0
0
## END

#### nounset
echo "[$unset]"
set -o nounset
echo "[$unset]"
echo end  # never reached
## stdout: []
## status: 1
## OK dash status: 2

#### -u is nounset
echo "[$unset]"
set -u
echo "[$unset]"
echo end  # never reached
## stdout: []
## status: 1
## OK dash status: 2

#### nounset with "$@"
set a b c
set -u  # shouldn't touch argv
echo "$@"
## stdout: a b c

#### set -u -- clears argv
set a b c
set -u -- # shouldn't touch argv
echo "$@"
## stdout: 

#### set -u -- x y z
set a b c
set -u -- x y z
echo "$@"
## stdout: x y z

#### reset option with long flag
set -o errexit
set +o errexit
echo "[$unset]"
## stdout: []
## status: 0

#### reset option with short flag
set -u 
set +u
echo "[$unset]"
## stdout: []
## status: 0

#### set -eu (flag parsing)
set -eu 
echo "[$unset]"
echo status=$?
## stdout-json: ""
## status: 1
## OK dash status: 2

#### -n for no execution (useful with --ast-output)
# NOTE: set +n doesn't work because nothing is executed!
echo 1
set -n
echo 2
set +n
echo 3
## stdout-json: "1\n"
## status: 0

#### pipefail
# NOTE: the sleeps are because osh can fail non-deterministically because of a
# bug.  Same problem as PIPESTATUS.
{ sleep 0.01; exit 9; } | { sleep 0.02; exit 2; } | { sleep 0.03; exit 0; }
echo $?
set -o pipefail
{ sleep 0.01; exit 9; } | { sleep 0.02; exit 2; } | { sleep 0.03; exit 0; }
echo $?
## stdout-json: "0\n2\n"
## status: 0
## N-I dash stdout-json: "0\n"
## N-I dash status: 2

#### shopt -p -o
shopt -po nounset
set -u
shopt -po nounset
## stdout-json: "set +o nounset\nset -o nounset\n"
## N-I dash/mksh stdout-json: ""
## N-I dash/mksh status: 127

#### shopt -p
shopt -p nullglob
shopt -s nullglob
shopt -p nullglob
## stdout-json: "shopt -u nullglob\nshopt -s nullglob\n"
## N-I dash/mksh stdout-json: ""
## N-I dash/mksh status: 127

#### noclobber off
set -o errexit
echo foo > $TMP/can-clobber
set +C
echo foo > $TMP/can-clobber
set +o noclobber
echo foo > $TMP/can-clobber
cat $TMP/can-clobber
## stdout: foo

#### noclobber on
# Not implemented yet.
rm $TMP/no-clobber
set -C
echo foo > $TMP/no-clobber
echo $?
echo foo > $TMP/no-clobber
echo $?
## stdout-json: "0\n1\n"
## OK dash stdout-json: "0\n2\n"

#### SHELLOPTS is updated when options are changed
echo $SHELLOPTS | grep -q xtrace
echo $?
set -x
echo $SHELLOPTS | grep -q xtrace
echo $?
set +x
echo $SHELLOPTS | grep -q xtrace
echo $?
## stdout-json: "1\n0\n1\n"
## N-I dash/mksh stdout-json: "1\n1\n1\n"

#### SHELLOPTS is readonly
SHELLOPTS=x
echo status=$?
## stdout: status=1
## N-I dash/mksh stdout: status=0

#### set -o lists options
# NOTE: osh doesn't use the same format yet.
set -o | grep -o errexit
## STDOUT:
errexit
## END

#### set without args lists variables
__GLOBAL=g
f() {
  local __mylocal=L
  local __OTHERLOCAL=L
  __GLOBAL=mutated
  set | grep '^__'
}
g() {
  local __var_in_parent_scope=D
  f
}
g
## status: 0
## STDOUT:
__GLOBAL='mutated'
__OTHERLOCAL='L'
__mylocal='L'
__var_in_parent_scope='D'
## END
## OK bash STDOUT:
__GLOBAL=mutated
__OTHERLOCAL=L
__mylocal=L
__var_in_parent_scope=D
## END
## OK mksh STDOUT:
__GLOBAL=mutated
__var_in_parent_scope=D
__OTHERLOCAL=L
__mylocal=L
## END

#### 'set' and 'eval' round trip

# NOTE: not testing arrays and associative arrays!
_space='[ ]'
_whitespace=$'[\t\r\n]'
_sq="'single quotes'"
_backslash_dq="\\ \""
_unicode=$'[\u03bc]'

# Save the variables
varfile=$TMP/vars-$(basename $SH).txt

set | grep '^_' > "$varfile"

# Unset variables
unset _space _whitespace _sq _backslash_dq _unicode
echo [ $_space $_whitespace $_sq $_backslash_dq $_unicode ]

# Restore them

. $varfile
echo "Code saved to $varfile" 1>&2  # for debugging

test "$_space" = '[ ]' && echo OK
test "$_whitespace" = $'[\t\r\n]' && echo OK
test "$_sq" = "'single quotes'" && echo OK
test "$_backslash_dq" = "\\ \"" && echo OK
test "$_unicode" = $'[\u03bc]' && echo OK

## STDOUT:
[ ]
OK
OK
OK
OK
OK
## END

#### set without args lists array variables
declare -a __array
__array=(1 2 '3 4')
set | grep '^__'
## STDOUT:
__array=([0]="1" [1]="2" [2]="3 4")
## END
## OK mksh STDOUT:
__array[0]=1
__array[1]=2
__array[2]='3 4'
## N-I dash stdout-json: ""
## N-I dash status: 2

#### set without args lists assoc array variables
typeset -A __assoc
__assoc['k e y']='v a l'
__assoc[a]=b
set | grep '^__'
## STDOUT:
__assoc=(["k e y"]="v a l" [a]="b" )
## END
## N-I mksh stdout-json: ""
## N-I mksh status: 1
## N-I dash stdout-json: ""
## N-I dash status: 1

#### shopt -q
shopt -q nullglob
echo nullglob=$?

# set it
shopt -s nullglob

shopt -q nullglob
echo nullglob=$?

shopt -q nullglob failglob
echo nullglob,failglob=$?

# set it
shopt -s failglob
shopt -q nullglob failglob
echo nullglob,failglob=$?

## STDOUT:
nullglob=1
nullglob=0
nullglob,failglob=1
nullglob,failglob=0
## END
## N-I dash/mksh STDOUT:
nullglob=127
nullglob=127
nullglob,failglob=127
nullglob,failglob=127
## END

#### shopt -q invalid
shopt -q invalidZZ
echo invalidZZ=$?
## STDOUT:
invalidZZ=2
## END
## OK bash STDOUT:
invalidZZ=1
## END
## N-I dash/mksh STDOUT:
invalidZZ=127
## END

#### sh -i loads rcfile (when combined with -c)
$SH -c 'echo 1'
cat >$TMP/rcfile <<EOF
echo RCFILE
EOF
$SH --rcfile $TMP/rcfile -i -c 'echo 2'
## STDOUT:
1
RCFILE
2
## END
## N-I dash/mksh STDOUT:
1
## END
## N-I dash status: 2
## N-I mksh status: 1
