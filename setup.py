"""Установка Screen Recorder & Transcriber."""
from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

with open("requirements.txt", "r", encoding="utf-8") as f:
    install_requires = [
        line.strip()
        for line in f
        if line.strip() and not line.startswith("#") and not line.startswith("-")
    ]

setup(
    name="screen-recorder-transcriber",
    version="1.1.0",
    description=(
        "Screen Recorder & Transcriber for Ubuntu: "
        "запись экрана, транскрибация, суммаризация, "
        "поиск и интеграция с Bitrix24"
    ),
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Alexander Krasnov",
    license="MIT",
    packages=find_packages(include=["src", "src.*"]),
    include_package_data=True,
    package_data={
        "": [
            "../resources/*",
            "../resources/icons/*",
            "../README.md",
        ],
    },
    install_requires=install_requires,
    extras_require={
        "dev": [
            "pytest>=7.4.0",
            "pytest-cov>=4.1.0",
            "pytest-qt>=4.2.0",
            "pytest-asyncio>=0.21.0",
            "pytest-mock>=3.11.0",
            "pytest-xdist>=3.3.0",
            "pylint>=2.17.0",
            "black>=23.0.0",
            "ruff>=0.4.0",
            "mypy>=1.10.0",
        ],
        "pdf": ["pypdf>=4.0"],
        "fuzzy": ["rapidfuzz>=3.0"],
    },
    entry_points={
        "console_scripts": [
            "screen-recorder=src.main:main",
        ],
        "gui_scripts": [
            "screen-recorder-gui=src.main:main",
        ],
    },
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Environment :: X11 Applications :: Qt",
        "Intended Audience :: End Users/Desktop",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Multimedia :: Sound/Audio :: Capture/Recording",
        "Topic :: Multimedia :: Video :: Capture",
        "Topic :: Office/Business",
    ],
)