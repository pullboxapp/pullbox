"""Native Metron price rows. Keep the decimal spelling; a country is not a currency."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pullbox.core.metroninfo import MAX_METRONINFO_FIELD_LENGTH


class IssuePriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    country: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$", strict=True)
    amount: str = Field(
        min_length=1,
        max_length=MAX_METRONINFO_FIELD_LENGTH,
        pattern=r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$",
        strict=True,
    )

    def label(self) -> str:
        return f"{self.country} {self.amount}"


type IssuePriceList = Annotated[tuple[IssuePriceEntry, ...], Field(min_length=1, max_length=128)]
PRICE_LIST: TypeAdapter[tuple[IssuePriceEntry, ...]] = TypeAdapter(IssuePriceList)
