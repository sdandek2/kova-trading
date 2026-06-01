from pydantic import BaseModel


class AccountInfo(BaseModel):
    portfolio_value: float
    cash: float
    buying_power: float
    daytrading_buying_power: float = 0.0
    tradeable_cash: float = 0.0
    raw_cash: float = 0.0
    day_pl: float
    day_pl_percent: float
