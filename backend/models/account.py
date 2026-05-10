from pydantic import BaseModel


class AccountInfo(BaseModel):
    portfolio_value: float
    cash: float
    buying_power: float
    day_pl: float
    day_pl_percent: float
