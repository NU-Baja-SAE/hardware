"""Schema for harness definitions. Validated via Pydantic."""
from __future__ import annotations
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


class Pin(BaseModel):
    n: int
    name: Optional[str] = None
    signal: Optional[str] = None


class BaseComponent(BaseModel):
    id: str = ""  # populated from dict key by parser
    label: Optional[str] = None
    position: Optional[tuple[float, float]] = None  # manual layout override
    near: Optional[str] = None  # layout hint: place near another component
    zone: Optional[str] = None  # logical zone: engine, cabin, chassis, etc.


class Connector(BaseComponent):
    type: Literal["connector", "device"]
    pins: list[Pin]

    def pin_numbers(self) -> list[int]:
        return [p.n for p in self.pins]

    def find_pin(self, ref: Union[int, str]) -> Optional[Pin]:
        """Find a pin by number or name."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            n = int(ref)
            return next((p for p in self.pins if p.n == n), None)
        return next((p for p in self.pins if p.name == ref), None)


class Bulkhead(BaseComponent):
    type: Literal["bulkhead"]
    positions: int  # number of pin pairs; generates A1..An and B1..Bn

    def side_pins(self, side: str) -> list[str]:
        return [f"{side}{i}" for i in range(1, self.positions + 1)]


class Splice(BaseComponent):
    type: Literal["splice"]
    pin_count: int = Field(ge=2)


Component = Union[Connector, Bulkhead, Splice]


class Wire(BaseModel):
    from_: str = Field(alias="from")
    to: str
    gauge: Optional[float] = None  # AWG
    color: Optional[str] = None
    length: Optional[float] = None  # mm
    signal: Optional[str] = None

    model_config = {"populate_by_name": True}


class Metadata(BaseModel):
    name: str = "Untitled Harness"
    revision: Optional[int] = None
    notes: Optional[str] = None


class Harness(BaseModel):
    metadata: Metadata = Metadata()
    components: dict[str, Component]
    wires: list[Wire]

    @model_validator(mode="after")
    def populate_ids(self):
        for cid, comp in self.components.items():
            comp.id = cid
        return self

    def get_component(self, cid: str) -> Component:
        if cid not in self.components:
            raise KeyError(f"Unknown component: {cid}")
        return self.components[cid]
