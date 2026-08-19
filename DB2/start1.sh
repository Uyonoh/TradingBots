#!/bin/bash

source ../.venv/bin/activate
python manager.py USA30 --tp-pips 45 --interval 900 --foreign --exclude '*'
