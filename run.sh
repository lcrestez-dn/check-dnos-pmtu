#! /bin/bash

topdir=$(cd "$(dirname "${BASH_SOURCE[0]}")"; pwd -P)
exec tox -c "$topdir" -e run -- "$@"
