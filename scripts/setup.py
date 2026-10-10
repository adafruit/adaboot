# SPDX-License-Identifier: Apache-2.0

import setuptools
from imgtool import imgtool_version

setuptools.setup(
    name="imgtool",
    version=imgtool_version,
    author="The MCUboot committers",
    author_email="mcuboot@groups.io",
    description=("MCUboot's image signing and key management"),
    license="Apache Software License",
    url="http://github.com/mcu-tools/mcuboot",
    packages=setuptools.find_packages(),
    # ADABOOT-CHANGE: Python 3.12+ (was 3.8+).
    python_requires='>=3.12',
    install_requires=[
        'cryptography>=40.0.0',
        'intelhex>=2.2.1',
        'click',
        'cbor2',
        'pyyaml',
    ],
    entry_points={
        "console_scripts": ["imgtool=imgtool.main:imgtool"]
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "Development Status :: 4 - Beta",
        "Topic :: Software Development :: Build Tools",
        "License :: OSI Approved :: Apache Software License",
    ],
)
