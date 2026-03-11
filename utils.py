"""Small standalone utility functions."""

import binascii
import os


def random_hex(length):
    """Generate a random hex string of the given byte-length."""
    return binascii.b2a_hex(os.urandom(length)).decode()
