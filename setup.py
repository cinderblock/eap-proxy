"Setup script for eap_proxy."
from setuptools import setup


setup(
    name="eap_proxy",
    version="2.0.0",
    description="Relay AT&T 802.1X (EAPOL) on UniFi OS gateways.",
    url="https://github.com/cinderblock/eap-proxy",
    author="Jay Soffian",
    license="BSD",
    py_modules=["eap_proxy"],
    entry_points={"console_scripts": ["eap_proxy=eap_proxy:main"]},
)
