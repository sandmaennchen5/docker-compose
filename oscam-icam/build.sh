#!/bin/sh

set -e

echo "Cloning Oscam..."
git clone https://git.streamboard.tv/common/oscam.git
cd oscam

echo "Applying ICAM patch..."
git apply ../patches/oscam_emu_icam_dvbapi.patch

echo "Building Oscam..."
make -j4

echo "Copying binary..."
cp oscam ../oscam

echo "Done."
