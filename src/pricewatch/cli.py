"""PriceWatch command-line entry point."""

import typer

app = typer.Typer(
    name="pricewatch",
    help="Operate the PriceWatch service and its local data.",
    no_args_is_help=True,
)
