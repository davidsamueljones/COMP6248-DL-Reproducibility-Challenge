from setuptools import setup, find_packages

exec(open("ssdn/version.py").read())

setup(
    name="ssdn",
    version=__version__,  # noqa
    packages=find_packages(),
    entry_points={"console_scripts": ["ssdn = ssdn.__main__:start_cli"]},
    install_requires=[
        "nptyping",
        "h5py",
        "imagesize",
        "overrides",
        "colorlog",
        "colored_traceback",
        "tqdm"
    ],
)
