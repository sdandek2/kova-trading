import Foundation

struct RiskSettings: Codable {
    var daily_loss_limit_pct: Double
    var stop_loss_pct: Double
    var take_profit_pct: Double
    var min_daily_trades: Int
    var afternoon_pressure_hour: Int
    var max_trades_per_cycle: Int
    var max_penny_position_pct: Double
    var cycle_interval_seconds: Int

    // Safe defaults — mirrors backend _RISK_DEFAULTS (aggressive mode)
    static let defaults = RiskSettings(
        daily_loss_limit_pct: 6.0,
        stop_loss_pct: 0.05,
        take_profit_pct: 0.20,
        min_daily_trades: 3,
        afternoon_pressure_hour: 13,
        max_trades_per_cycle: 3,
        max_penny_position_pct: 3.0,
        cycle_interval_seconds: 600
    )

    init(daily_loss_limit_pct: Double = 6.0,
         stop_loss_pct: Double = 0.05,
         take_profit_pct: Double = 0.20,
         min_daily_trades: Int = 3,
         afternoon_pressure_hour: Int = 13,
         max_trades_per_cycle: Int = 3,
         max_penny_position_pct: Double = 3.0,
         cycle_interval_seconds: Int = 600) {
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.min_daily_trades = min_daily_trades
        self.afternoon_pressure_hour = afternoon_pressure_hour
        self.max_trades_per_cycle = max_trades_per_cycle
        self.max_penny_position_pct = max_penny_position_pct
        self.cycle_interval_seconds = cycle_interval_seconds
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        daily_loss_limit_pct    = (try? c.decode(Double.self, forKey: .daily_loss_limit_pct))    ?? 6.0
        stop_loss_pct           = (try? c.decode(Double.self, forKey: .stop_loss_pct))           ?? 0.05
        take_profit_pct         = (try? c.decode(Double.self, forKey: .take_profit_pct))         ?? 0.20
        min_daily_trades        = (try? c.decode(Int.self,    forKey: .min_daily_trades))        ?? 3
        afternoon_pressure_hour = (try? c.decode(Int.self,    forKey: .afternoon_pressure_hour)) ?? 13
        max_trades_per_cycle    = (try? c.decode(Int.self,    forKey: .max_trades_per_cycle))    ?? 3
        max_penny_position_pct  = (try? c.decode(Double.self, forKey: .max_penny_position_pct))  ?? 3.0
        cycle_interval_seconds  = (try? c.decode(Int.self,    forKey: .cycle_interval_seconds))  ?? 600
    }
}
