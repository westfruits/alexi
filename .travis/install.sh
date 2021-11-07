#!/bin/bash

set -e
set -x

if [[ "${TOX_ENV}" == "pypy" ]]; then
    sudo add-apt-repository -y ppa:pypy/ppa
fi

sudo apt-get -y update

if [[ "${TOX_ENV}" == "pypy" ]]; then
    sudo apt-get install -y pypy

    # This is required because we need to get rid of the Travis installed PyPy
    # or it'll take precedence over the PPA installed one.
    sudo rm -rf /usr/local/pypy/bin
fi
