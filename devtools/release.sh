#!/bin/bash
#
# The big Oil release process.
#
# Usage:
#   devtools/release.sh <function name>
#
# Pre-release:
#   demo/osh-debug.sh osh-for-release: Start a shell to dogfood
#   opy/regtest.sh verify-golden, because that one tends to be flaky
#   build/cpython-defs.sh {rebuild-manifest,oil-py-names,filter-methods}
#     (regenerate C source)
#
# Steps:
#   build/doc.sh update-src-versions  (optional)
#   $0 build-and-test  (builds tarball, runs unit/gold/osh2oil suites, etc.)
#     prereq: build/codegen.sh {download,install}-re2c
#     test/gold.sh run-for-release (outside OSH_HIJACK_SHEBANG)
#   $0 metrics  # this can catch bugs
#   test/wild.sh all (3-4 minutes on fast machine, outside OSH_HIJACK_SHEBANG)
#   $0 test-opy (2 minutes on fast machine)
#   $0 spec-all  # tests 2 OSH binaries
#   benchmarks:
#     Sync up oilshell/benchmark-data repo.
#     flanders: $0 benchmark-build, then $0 benchmark-run
#     - for stability, restart flanders
#     - prereqs:
#       - benchmarks/osh-runtime.sh {download,extract}
#       - benchmarks/ovm-build.sh {download,extract-oil,extract-other}
#       - build/codegen.sh {download,extract}-clang (for OVM build benchmarks)
#     lisa: $0 benchmark-run
#     Commit files to oilshell/benchmark-data repo and sync.
#   benchmarks/report.sh all
#   $0 deploy-tar  # needed to checksum
#   $0 build-tree
#   $0 compress
#   devtools/release-version.sh git-changelog-$VERSION
#   devtools/release-version.sh announcement-$VERSION
#   MAYBE: ./local.sh test-release-tree if you want to preview it
#   $0 sync-old-tar (for releases.html)
#   $0 deploy-doc
#
#   demo/osh-debug.sh analyze  # see what you ran
# 
# - Go to oilshell.org__deploy and "git add release/$VERSION".
# - Go to oilshell.org repo and do ./deploy.sh site.

set -o nounset
set -o pipefail
set -o errexit

readonly OIL_VERSION=$(head -n 1 oil-version.txt)

# Dir is defined in build/test.sh.
readonly OSH_RELEASE_BINARY=_tmp/oil-tar-test/oil-$OIL_VERSION/_bin/osh

source devtools/common.sh  # html-footer

log() {
  echo "$@" 1>&2
}

# For redoing a release.  This is everything until you have to 'git pull' the
# benchmark-data repo to make reports.
auto-machine1() {
  $0 build-and-test
  $0 metrics  # this can catch bugs
  test/wild.sh all
  $0 test-opy
  $0 spec-all
  $0 benchmark-run
  #$0 benchmark-run-on-1-machine
}

# TODO:
# - enforce that there is a release/$VERSION branch?

# oilshell.org__deploy/
#   releases.html
#   opy-releases.html  (later)
#   release/
#     $VERSION/
#       index.html  # release page, from doc/release-index.md
#       oil-version.txt
#       release-date.txt
#       announcement.html  # HTML redirect
#       changelog.html
#       doc/
#         INSTALL.html
#         osh-quick-ref.html
#       test/  # results
#         spec.wwz/
#           machine-lisa/
#         wild.wwz/
#         unit.wwz/
#         other.wwz/
#           osh2oil.txt
#           gold.txt
#           parse-errors.txt
#           runtime-errors.txt
#           oshc-deps.txt
#           osh-usage.txt
#           arena.txt
#         tarball/  # log of building and running the tarball?
#       asan/       # spec tests or other?
#                   # or it can be put under test/{spec,wild}
#       metrics.wwz/  # static metrics on source code?
#         line-counts/
#           nativedeps.txt (build/stats.sh line counts)
#         bytecode size, number of PyCodeObject
#         number of functions, classes, etc.?
#         bytecode/bundle size (binary size on x86_64 is in ovm-build.sh)
#         tarball size?
#       coverage/  # coverage of all spec tests?  And gold tests maybe?
#         python/  # python stdlib coverage  with pycoverage
#         c/       # c coverage with gcc/clang
#       benchmarks.wwz/
#         osh-parser/
#         osh-runtime/
#         vm-baseline/
#         oheap/
#         ...
#         startup/
#   download/  # What about native binaries?
#     0.0.0/  
#       oil-0.0.0.tar.xz 

_clean-tmp-dirs() {
  rm -r -f \
    _tmp/{spec,unit,gold,parse-errors,osh2oil,wild/www} \
    _tmp/metrics \
    _tmp/opy-test \
    _tmp/{osh-parser,osh-runtime,vm-baseline,ovm-build,oheap} \
    _tmp/oil-tar-test
}

_clean() {
  _clean-tmp-dirs   # Remove benchmark stuff
  rm -r -f _devbuild  # We're redoing the dev build
  build/actions.sh clean-repo
}

_dev-build() {
  build/dev.sh all  # for {libc,fastlex}.so, needed to crawl deps
}

_release-build() {
  build/prepare.sh configure
  build/prepare.sh build-python

  # Build the oil tar
  $0 oil

  # Build the binary out of the tar.
  build/test.sh oil-tar

  ln -s -f --no-target-directory -v oil.ovm $OSH_RELEASE_BINARY

  # TODO: Move these?

  # _pending/oil-alpha1
  # _tmp/pending/
  #    oil-0.5.alpha2.tar.gz
  #    osh ->
  #    oil-0.5.alpha2/
  #      _bin/
  #         oil.ovm
}

readonly HAVE_ROOT=1

readonly -a OTHER_TESTS=(
  gold 
  osh2oil 
  parse-errors runtime-errors
  arena
  osh-usage oshc-deps
  opyc
)

run-other-tests() {
  for name in "${OTHER_TESTS[@]}"; do
    case $name in
      gold)
        if test -n "${OSH_HIJACK_SHEBANG:-}"; then
          cat >&2 <<'EOF'
=====
WARNING: Skipping gold tests because $OSH_HIJACK_SHEBANG is set.'
Run them manually with:

  test/gold.sh run-for-release
=====
EOF
        fi
        continue
        ;;
      *)
        banner "Test suite: $name"
        ;;
    esac

    test/$name.sh run-for-release
  done
}

_test-release-build() {
  # NOTE: Need test/alpine.sh download;extract;setup-dns,add-oil-build-deps,
  # etc.

  if test -n "$HAVE_ROOT"; then
    # TODO: Factor out test/alpine.sh to test/chroot.sh
    test/alpine.sh copy-tar '' oil
    test/alpine.sh test-tar '' oil
  fi

  test/spec.sh smoke  # Initial smoke test, slightly redundant.

  run-other-tests

  # Just test the release build (not under CPython.  That comes later.)
  OSH_LIST="$OSH_RELEASE_BINARY" test/spec.sh all
}

# NOTE: Following opy/README.md.  Right now this is a quick and dirty
# verification.  For example we found out something about the golden checksums
# for the OPy regtest!
test-opy() {
  local out=$PWD/_tmp/test-opy

  mkdir -p $out

  metrics/source-code.sh oil-python-symbols $out
  metrics/source-code.sh opy-python-symbols $out

  pushd opy

  local step=''

  step='build-oil-repo'
  echo "--- $step ---"
  time ./build.sh oil-repo > $out/$step.txt 2>&1
  echo $?

  step='test-gold'
  echo "--- $step ---"
  time ./test.sh gold > $out/$step.txt 2>&1
  echo $?

  # NOTE: This is sensitive to Python 2.7.12 vs .13 vs .14.  Ideally we would
  # remove that.
  # NOTE: There is no indication if this fails!
  ./regtest.sh compile-all > $out/regtest-compile.txt
  ./regtest.sh verify-golden > $out/regtest-verify-golden.txt

  popd
}

spec-all() {
  OSH_LIST="bin/osh $OSH_RELEASE_BINARY" test/spec.sh all
}

# For quickly debugging failures that don't happen in dev mode.
spec-one() {
  OSH_LIST="bin/osh $OSH_RELEASE_BINARY" test/spec.sh "$@"
}


# TODO: Log this whole thing?  Include logs with the /release/ page?
build-and-test() {
  # 5 steps: clean, dev build, unit tests, release build, end-to-end tests.

  # Before doing anything
  test/lint.sh travis

  _clean
  _dev-build
  test/unit.sh run-for-release
  _release-build
  _test-release-build

  # We're now ready to run 'benchmarks/auto.sh all'.
}

_install() {
  test/spec.sh install-shells

  # A subset of build/dev.sh ubuntu-deps.  (Do we need build-essential?)
  sudo apt install python-dev
}

# Run before benchmarks/auto.sh all.  We just build, and assume we tested.
benchmark-build() {
  if test -n "$HAVE_ROOT"; then
    _install
  fi
  _clean
  _dev-build

  _release-build
}

# Run benchmarks with the binary built out of the tarball.
benchmark-run() {
  OSH_OVM=$OSH_RELEASE_BINARY benchmarks/auto.sh all
}

_compressed-tarball() {
  local name=${1:-hello}
  local version=${2:-0.0.0}

  local in=_release/$name.tar
  local out=_release/$name-$version.tar.gz

  # Overwrite it to cause rebuild of oil.tar
  build/actions.sh write-release-date

  #make -d -r $in  # To debug
  make $in
  time gzip -c $in > $out
  ls -l $out

  # xz version is considerably smaller.  1.15 MB  vs. 1.59 MB.
  local out2=_release/$name-$version.tar.xz
  time xz -c $in > $out2
  ls -l $out2
}

oil() {
  _compressed-tarball oil $OIL_VERSION
}

hello() {
  _compressed-tarball hello $(head -n 1 build/testdata/hello-version.txt)
}


_link() {
  ln -s -f -v --no-target-directory "$@"
}

compress() {
  local root=$PWD/_release/VERSION/

  log "--- test/other"
  local out="$root/test/other.wwz"
  pushd _tmp/other
  time zip -r -q $out .  # recursive, quiet
  popd

  log "--- test/unit"
  local out="$root/test/unit.wwz"
  pushd _tmp/unit
  time zip -r -q $out .  # recursive, quiet
  popd

  log "--- test/spec"
  local out="$root/test/spec.wwz"
  pushd _tmp/spec
  time zip -r -q $out .  # recursive, quiet
  popd

  log "--- test/wild"
  local out="$root/test/wild.wwz"
  pushd _tmp/wild/www
  time zip -r -q $out .  # recursive, quiet
  popd

  log "--- test/opy"
  local out="$root/test/opy.wwz"
  pushd _tmp/test-opy
  time zip -r -q $out .  # recursive, quiet
  popd

  log "--- metrics"
  local out="$root/metrics.wwz"
  pushd _tmp/metrics
  time zip -r -q $out .  # recursive, quiet
  popd

  compress-benchmarks

  tree _release/VERSION
}

compress-benchmarks() {
  local root=$PWD/_release/VERSION/
  mkdir -p $root

  log "--- benchmarks"

  local out="$root/benchmarks.wwz"

  # Technically we only need index.html.  But it's nice to have stage1 and
  # stage2 in case we need backup.

  pushd _tmp
  find \
    osh-parser/{stage1,stage2,index.html} \
    osh-runtime/{stage1,stage2,index.html} \
    vm-baseline/{stage1,stage2,index.html} \
    ovm-build/{stage1,stage2,index.html} \
    -type f \
    | xargs --verbose -- zip -q $out 
  popd
    #oheap/{stage1,stage2,index.html} \
}

line-counts() {
  local out=$1  # should be an absolute path
  mkdir -p $out

  # Counting directly from the build.
  metrics/tarball.sh linecount-pydeps > $out/pydeps.txt
  metrics/tarball.sh linecount-nativedeps > $out/nativedeps.txt

  # My arbitrary categorization.
  metrics/source-code.sh all > $out/src.txt  # Count repo lines

  metrics/source-code.sh osh-cloc > $out/osh-cloc.txt

  local opy_out=$out/opy.txt
  pushd opy
  ./count.sh all > $opy_out
  popd
}

metrics() {
  local out=_tmp/metrics
  mkdir -p $out

  line-counts $PWD/$out/line-counts

  metrics/bytecode.sh run-for-release
  metrics/native-code.sh run-for-release
  build/cpython-defs.sh run-for-release

  tree $out
}

_copy-path() {
  local src=$1 dest=$2
  mkdir -p $(dirname $dest)
  cp -v $src $dest
}

copy-web() {
  find web \
    \( -name _tmp -a -prune \) -o \
    \( -name '*.css' -o -name '*.js' \) -a -printf '%p _release/VERSION/%p\n' |
  xargs -n 2 -- $0 _copy-path
}

this-release-links() {
  echo '<div style="margin-left: 2em;">'
  echo '<table class="file-table">'
  _tarball-links-row-html "$OIL_VERSION"
  echo '</table>'
  echo '</div>'
}

# Turn HTML comment into a download link
add-download-links() {
  awk -v snippet="$(this-release-links)" '
    /<!-- REPLACE_WITH_DOWNLOAD_LINKS -->/ {
      print(snippet)
      next
    }

    # Everything else
    { print }
  '
}

# TODO:
# Test out web/ *.css,js,html
# metrics/line-counts.wwz/
#   src.txt
#   pydeps.txt
#   nativedeps.txt

build-tree() {
  local root=_release/VERSION
  mkdir -p $root/{doc,test}

  # Metadata
  cp -v _build/release-date.txt oil-version.txt $root

  # Docs

  # NOTE: This action is also run in the build.  It generates code that goes in
  # the binary.
  build/doc.sh osh-quick-ref _release/VERSION

  build/doc.sh install
  build/doc.sh release-index _tmp/release-index.html
  add-download-links < _tmp/release-index.html > $root/index.html

  # Problem: You can't preview it without .wwz!
  # Maybe have local redirects VERSION/test/wild/ to 
  #
  # Instead of linking, I should compress them all here.

  copy-web

  tree $root
}

deploy-doc() {
  local deploy_repo='../oilshell.org__deploy'
  local release_root_dir="$deploy_repo/release"
  local release_dir="$release_root_dir/$OIL_VERSION"

  cp -v -r --force --no-target-directory \
    _release/VERSION/ $release_dir/

  # Generate release index.
  html-index $release_root_dir _tmp/releases.html
  cp -v _tmp/releases.html $deploy_repo

  tree -L 3 $release_root_dir
  
  ls -l $deploy_repo/releases.html
}

readonly DOWNLOAD_DIR='../oilshell.org__deploy/download/'

# Generating releases.html requires the old tarballs!
sync-old-tar() {
  local user=$1  # required username
  rsync --archive --verbose \
    $user@oilshell.org:oilshell.org/download/ $DOWNLOAD_DIR
}

# I think these aren't checked into git?  They can just be managed separately?
# Or should you check in the sha checksums?  Those will be in releases.html,
# but a CSV might be nice.
deploy-tar() {
  mkdir -p $DOWNLOAD_DIR

  cp -v _release/oil-$OIL_VERSION.tar.* $DOWNLOAD_DIR

  ls -l $DOWNLOAD_DIR
}

#
# Generate releases.html.
#

# Examples of similar release HTML pages:
# - https://golang.org/dl/  -  "Older versions", sha1 / sha256.
# - Python has all point releases in chronological order, and then a separate
# page for each changelog.  There is too much boilerplate maybe?
#   - It has release notes before the downloads.  Not sure I like that.
# - node.js: https://nodejs.org/en/
#   - user agent detection for the right binary -- meh I don't want that
# - Ruby: https://www.ruby-lang.org/en/downloads/releases/
# - https://www.lua.org/download.html

pretty-size() {
  local path=$1
  stat --format '%s' "$path" | python -c '
import sys
num_bytes = int(sys.stdin.read())
print "{:,}".format(num_bytes)
'
}

# NOTE: It might be better to link to files like this in the /release/ tree.
# Although I am not signing them.

# https://nodejs.org/dist/v8.11.4/SHASUMS256.txt.asc

_tarball-links-row-html() {
  local version=$1

  cat <<EOF
<tr class="file-table-heading">
  <td></td>
  <td>File / SHA256 checksum</td>
  <td class="size">Size</td>
  <td></td>
</tr>
EOF

  for name in oil-$version.tar.{xz,gz}; do
    local url="/download/$name"  # The server URL
    local path="../oilshell.org__deploy/download/$name"
    local checksum=$(sha256sum $path | awk '{print $1}')
    local size=$(pretty-size $path)

    # TODO: Port this to oil with "commas" extension.

    # Three columns: date, version, and links
    cat <<EOF
    <tr> 
      <td></td>
      <td class="filename"><a href="$url">$name</a></td>
      <td class="size">$size</td>
    </tr>
    <tr>
      <td></td>
      <td colspan=2 class="checksum">$checksum</td>
    </tr>
EOF
  done
}

# Columns: Date / Version / Docs /    / Files
#                           Changelog  .xz
#                           Install
#                           Docs/
#
# The files could be a separate page and separate table?  I could provide
# pre-built versions eventually?  Linux static versions?

# TODO: Each of these would be a good candidate for a data frame!  Data vs.
# presentation.

# Simple UI:
# - home page shows latest version (source release for now, binary release later?)
#   - link to Changelog, INSTALL, doc index
# - or see all releases
# - Grey out older releases?

# TODO: Should be sorted by date?  How to do that, with bash array?  Or Awk?
# $timestamp $version $timestamp file?  And then sort -n  I guess?  Change
# the release date format.  It will use Unix timestamp (OK until 2038!)

_html-index() {
  local release_root_dir=$1 # the directory we want to make an index of

  for entry in $release_root_dir/*; do
    if ! test -d $entry; then
      continue
    fi
    local dir=$entry

    local version=$(head -n 1 $dir/oil-version.txt)
    local release_date=$(head -n 1 $dir/release-date.txt)

    log "-- $dir"
    log "Version: $version"
    log "Release Date: $release_date"
    log ""

    echo "$release_date $version"
  done > _tmp/release-meta.txt

  # Reverse sort by release date
  sort -r _tmp/release-meta.txt > _tmp/sorted-releases.txt

  while read date _ version; do
    log "Release Date: $date"
    log "Version: $version"

    # anchor
    cat <<EOF
<tr>
  <td>
    <span class="date">$date</span>
  </td>
  <td>
    <a name="$version"></a>
    <span class="version-number">$version</span>
  </td>
  <td>
    <p>                <a href="release/$version/announcement.html">Announcement</a>
       &nbsp; | &nbsp; <a href="release/$version/doc/INSTALL.html">INSTALL</a>
       &nbsp; | &nbsp; <a href="release/$version/">Docs and Details</a>
    </p>
  </td>
</tr>
EOF

    _tarball-links-row-html $version

    cat <<EOF
<tr>
  <td colspan="3">
    <div style="padding: 1em;" >
    </div>
  </td>
</tr>

EOF

  done < _tmp/sorted-releases.txt
}

_releases-html-header() {
  cat <<EOF
<!DOCTYPE html>
<html>
  <head>
    <title>Oil Releases</title>
    <style>
      /* Make it centered and skinny for readability */
      body {
        margin: 0 auto;
        width: 50em;
      }
      h1 {
        text-align: center;
      }
EOF

  cat web/release-index.css

cat <<EOF
      /* Copied from oilshell.org bundle.css */
      .date {
        font-size: medium;
        color: #555;
        padding-left: 1em;
      }
      #home-link {
        text-align: right;
      }
    </style>
  </head>
  <body>
    <p id="home-link">
      <a href="/">oilshell.org</a>
    </p>
    <h1>Oil Releases</h1>

    <table class="file-table">
EOF
}

html-index() {
  local release_root_dir=$1
  local out=${2:-_tmp/releases.html}

  { _releases-html-header
    _html-index $release_root_dir
    html-footer
  } > $out
  ls -l $out
}

# For quickly iterating on tarball size reductions.
tarball-size() {
  make clean-repo
  make _bin/oil.ovm-dbg  # faster way to build bytecode
  oil  # make tarball
  build/test.sh oil-tar  # Ctrl-C this, then run metrics/tarball.sh
}

# This is a hack because the Makefile dependencies aren't correct.
quick-oil-tarball() {
  make clean-repo
  make _bin/oil.ovm-dbg

  local in=_release/oil.tar
  local out=_release/oil-$OIL_VERSION.tar.gz

  make $in
  time gzip -c $in > $out
}

upload-tmp() {
  local tarball=$1
  local user=$2

  scp $tarball $user@oilshell.org:tmp/
}

sync-tmp() {
  local user=$1
  local dest=${2:-_tmp/candidates}
  mkdir -p $dest
  rsync --archive --verbose $user@oilshell.org:tmp/ $dest
}

"$@"
