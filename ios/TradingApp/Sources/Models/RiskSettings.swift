import Foundation

struct RiskSettings: Codable {
    var daily_loss_limit_pct: Double
    var stop_loss_pct: Double
    var take_profit_pct: Double
    var min_daily_trades: Int
    var afternoon_pressure_hour: Int

    // Safe defaults — mirrors backend _RISK_DEFAULTS (aggressive mode)
    static let defaults = RiskSettings(
        daily_loss_limit_pct: 6.0,
        stop_loss_pct: 0.05,
        take_profit_pct: 0.20,
        min_daily_trades: 4,
        afternoon_pressure_hour: 13
    )

    init(daily_loss_limit_pct: Double = 6.0,
         stop_loss_pct: Double = 0.05,
         take_profit_pct: Double = 0.20,
         min_daily_trades: Int = 4,
         afternoon_pressure_hour: Int = 13) {
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.min_daily_trades = min_daily_trades
        self.afternoon_pressure_hour = afternoon_pressure_hour
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        daily_loss_limit_pct  = (try? c.decode(Double.self, forKey: .daily_loss_limit_pct))  ?? 6.0
        stop_loss_pct         = (try? c.decode(Double.self, forKey: .stop_loss_pct))         ?? 0.05
        take_profit_pct       = (try? c.decode(Double.self, forKey: .take_profit_pct))       ?? 0.20
        min_daily_trades      = (try? c.decode(Int.self,    forKey: .min_daily_trades))      ?? 4
        afternoon_pressure_hour = (try? c.decode(Int.self,  forKey: .afternoon_pressure_hour)) ?? 13
    }
}
