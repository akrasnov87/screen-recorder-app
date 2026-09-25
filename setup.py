from setuptools import find_packages, setup

setup(
    name="screen-recorder-transcriber",
    version="1.0.0",
    description="Screen Recorder & Transcriber for Ubuntu",
    packages=find_packages(include=["src", "src.*"]),
    include_package_data=True,
    package_data={"": ["../resources/*", "../resources/icons/*"]},
    install_requires=[
        "PySide6>=6.4.0",
        "aiohttp>=3.8.0",
        "aiofiles>=23.0.0",
        "cryptography>=39.0.0",
        "keyring>=23.0.0",
        "pynput>=1.7.0",
    ],
    entry_points={
        "console_scripts": [
            "screen-recorder=src.main:main",
        ],
    },
    python_requires=">=3.10",
)