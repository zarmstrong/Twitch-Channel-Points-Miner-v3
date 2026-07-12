from os import path

import setuptools
import re


def read(fname):
    return open(path.join(path.dirname(__file__), fname), encoding="utf-8").read()


metadata = dict(
    re.findall(
        r"""__([a-z]+)__ = "([^"]+)""", read("TwitchChannelPointsMiner/__init__.py")
    )
)

setuptools.setup(
    name="Twitch-Channel-Points-Miner-v2",
    version=metadata["version"],
    author="Tkd-Alex (Alessandro Maggio) and rdavydov (Roman Davydov)",
    author_email="alex.tkd.alex@gmail.com",
    description="A simple script that will watch a stream for you and earn the channel points.",
    license="GPLv3+",
    keywords="python bot streaming script miner twtich channel-points",
    url="https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2",
    packages=setuptools.find_packages(),
    py_modules=["twitchdrops_app_scraper"],
    include_package_data=True,
    install_requires=[
        "requests==2.32.5",
        "websocket-client==1.7.0",
        "pillow==10.2.0",
        "python-dateutil==2.8.2",
        "emoji==2.10.1",
        "millify==0.1.1",
        "pre-commit==3.6.2",
        "colorama==0.4.6",
        "flask==3.0.2",
        "irc==20.4.0",
        "pandas==2.2.1",
        "pytz==2024.1",
        "validators==0.22.0",
    ],
    long_description=read("README.md"),
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Operating System :: OS Independent",
        "Topic :: Utilities",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Scientific/Engineering",
        "Topic :: Software Development :: Version Control :: Git",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Natural Language :: English",
    ],
    python_requires=">=3.9",
)
