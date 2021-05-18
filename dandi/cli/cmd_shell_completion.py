import os
from os.path import basename

import click
from packaging.version import Version

SHELLS = ["bash", "zsh", "fish"]


@click.command("shell-completion")
@click.option(
    "-s",
    "--shell",
    type=click.Choice(["auto"] + SHELLS),
    default="auto",
    help="The shell for which to generate completion code",
)
def shell_completion(shell):
    """ Emit command completion activation code """
    if shell == "auto":
        try:
            shell = basename(os.environ["SHELL"])
        except KeyError:
            raise click.UsageError(
                "Could not determine running shell: SHELL environment variable not set"
            )
        if shell not in SHELLS:
            raise click.UsageError(f"Unsupported/unrecognized shell {shell!r}")
    if Version(click.__version__) < Version("8.0.0"):
        varfmt = "source_{shell}"
    else:
        varfmt = "{shell}_source"
    os.environ["_DANDI_COMPLETE"] = varfmt.format(shell=shell)

    from .command import main

    main.main(args=[])
