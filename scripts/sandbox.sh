#!/usr/bin/env bash
# Compile v2's builds (scripts/mod_lib.py) in a sandbox, as build.yml's are (mod_build_sandbox.sh in
# Starling): a mod's code goes into the compiler, and `include_bytes!` reads any file it can see. So it
# runs with no network, unable to gain privileges, in namespaces of its own (made with sudo, given up
# before anything of the mods runs), over a home folder that holds only what the build needs, read-only
# but for the answers: none of the runner's own files, its workspace or its tokens.
#
#   sandbox.sh JOBS MODS SDK RESULT     as mod_lib.py's `build`, with BUILD and MOD_LIB_COMPILER
set -uo pipefail

jobs=$(realpath "$1") mods=$(realpath "$2") sdk=$(realpath "$3")
mkdir -p "$4"
result=$(realpath "$4")
scripts=$(realpath "$(dirname "$0")")
seconds=${SANDBOX_SECONDS:-3000}

sudo -n timeout --kill-after=10 "$seconds" \
  unshare --net --mount --pid --fork --kill-child --propagation private bash -euc '
home=$1 uid=$2 gid=$3 jobs=$4 mods=$5 sdk=$6 result=$7 scripts=$8 build=$9 compiler=${10} ndk=${11}
mount -t proc proc /proc
mount -t tmpfs -o mode=1777 tmpfs /tmp

# What the build may see, set aside before the home folder goes.
keep=/mnt
mount -t tmpfs tmpfs "$keep"
place() { mkdir -p "$keep/$1"; mount --bind "$2" "$keep/$1"; }
place rustup "$home/.rustup"
place cargo "$home/.cargo"
place scripts "$scripts"
place sdk "$sdk"
place mods "$mods"
place result "$result"
cp "$jobs" "$keep/jobs.json"

mount -t tmpfs -o "mode=755,uid=$uid,gid=$gid" tmpfs "$home"
back() {
  mkdir -p "$home/$2"
  mount --move "$keep/$1" "$home/$2"
  [ "$3" = rw ] || mount -o remount,bind,ro "$home/$2"
}
back rustup .rustup ro
back cargo .cargo ro
back scripts scripts ro
back sdk sdk ro
back mods mods ro
back result result rw
cp "$keep/jobs.json" "$home/jobs.json"
chown "$uid:$gid" "$home/jobs.json"
umount "$keep"

cd /tmp
exec setpriv --reuid="$uid" --regid="$gid" --clear-groups --no-new-privs \
  --bounding-set=-all --inh-caps=-all \
  env -i HOME="$home" PATH="$home/.cargo/bin:/usr/bin:/bin" CARGO_NET_OFFLINE=true LANG=C.UTF-8 \
  BUILD="$build" MOD_LIB_COMPILER="$compiler" ANDROID_NDK_HOME="$ndk" \
  python3 -I "$home/scripts/mod_lib.py" build "$home/jobs.json" "$home/mods" "$home/sdk" "$home/result"
' sandbox "$HOME" "$(id -u)" "$(id -g)" "$jobs" "$mods" "$sdk" "$result" "$scripts" "${BUILD:-}" \
  "${MOD_LIB_COMPILER:-}" "${ANDROID_NDK_HOME:-}"
