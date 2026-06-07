import SwiftUI

struct AnalyticsView: View {
    @State private var scorecard: [String: SideScorecard] = [:]
    @State private var blockedTrades: [BlockedTradeReport] = []
    @State private var varData: PortfolioVaR? = nil
    @State private var setupData: [SetupPerformance] = []
    @State private var aiBaseline: AIBaselineStats? = nil
    @State private var slippage: SlippageSummary? = nil
    @State private var hourData: [HourPerformance] = []
    @State private var tradingStatus: TradingStatus? = nil
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if isLoading && scorecard.isEmpty && tradingStatus == nil {
                LoadingView()
            } else if let error = errorMessage, scorecard.isEmpty {
                ErrorView(message: error) { await load() }
            } else {
                ScrollView {
                    VStack(spacing: 16) {
                        regimeDashboardSection
                        varSection
                        longShortScorecardSection
                        setupTypeSection
                        aiBaselineSection
                        slippageSection
                        timeOfDaySection
                        blockedTradesSection
                    }
                    .padding()
                }
                .refreshable { await load() }
            }
        }
        .navigationTitle("Analytics")
        .task { await load() }
    }

    // MARK: - A. Regime Dashboard

    private var regimeDashboardSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Market Regime")
                .font(.headline)

            if let status = tradingStatus, let regime = status.brainRegime {
                HStack(spacing: 16) {
                    VStack(spacing: 4) {
                        Text(regime.uppercased())
                            .font(.title2).fontWeight(.bold)
                            .foregroundStyle(regimeColor(regime))
                        Text("Regime")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)

                    VStack(spacing: 4) {
                        Text((status.vixLevel ?? "—").uppercased())
                            .font(.title2).fontWeight(.bold)
                            .foregroundStyle(vixColor(status.vixLevel ?? ""))
                        Text("VIX")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)

                    VStack(spacing: 4) {
                        if let mult = status.regimeCapitalMult {
                            Text(String(format: "%.0f%%", mult * 100))
                                .font(.title2).fontWeight(.bold)
                                .foregroundStyle(mult >= 1.0 ? .green : .orange)
                        } else {
                            Text("—").font(.title2).fontWeight(.bold)
                        }
                        Text("Size")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)

                    VStack(spacing: 4) {
                        if let conf = status.regimeConfidence {
                            Text(String(format: "%.0f%%", conf * 100))
                                .font(.title2).fontWeight(.bold)
                        } else {
                            Text("—").font(.title2).fontWeight(.bold)
                        }
                        Text("Confidence")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)
                }
                .padding(.vertical, 4)
            } else {
                Text("Regime detected after first trading cycle.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - E. Portfolio VaR

    private var varSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Portfolio Risk")
                .font(.headline)
            Text("If all positions move 1 ATR against you simultaneously")
                .font(.caption).foregroundStyle(.secondary)

            if let v = varData, v.varDollars != nil || v.grossDollars != nil {
                HStack(spacing: 0) {
                    VarStatView(
                        label: "Value at Risk",
                        dollars: v.varDollars,
                        pct: v.varPct,
                        color: varRiskColor(v.varPct)
                    )
                    Divider().frame(height: 50)
                    VarStatView(
                        label: "Gross Exposure",
                        dollars: v.grossDollars,
                        pct: v.grossPct,
                        color: (v.grossPct ?? 0) > 80 ? .red : .primary
                    )
                }
                if let asOf = v.asOf {
                    Text("Updated: \(shortTime(asOf))")
                        .font(.caption2).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
            } else {
                Text("Logged each cycle once positions are open.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - Long vs Short Scorecard

    private var longShortScorecardSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Long vs Short Scorecard")
                .font(.headline)

            if scorecard.isEmpty {
                Text("No closed trades yet — data appears after first position closes.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            } else {
                ForEach(["long", "short"], id: \.self) { side in
                    if let s = scorecard[side] {
                        ScorecardCard(side: side, scorecard: s)
                    }
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - B. Setup Type P&L

    private var setupTypeSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("P&L by Setup Type")
                .font(.headline)

            if setupData.isEmpty {
                Text("Tagged at entry — data appears after ~10 closed trades.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            } else {
                ForEach(setupData) { s in
                    SetupTypeRow(setup: s)
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - C. AI vs Signal Baseline

    private var aiBaselineSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("AI vs Signal Baseline")
                .font(.headline)
            Text("Does Claude add alpha or kill it?")
                .font(.caption).foregroundStyle(.secondary)

            if let b = aiBaseline, b.totalClosedTrades > 0 {
                HStack(spacing: 0) {
                    MiniStat(
                        label: "Claude Win Rate",
                        value: b.claudeWinRate.map { String(format: "%.0f%%", $0 * 100) } ?? "—",
                        color: (b.claudeWinRate ?? 0) >= 0.55 ? .green : .orange
                    )
                    MiniStat(
                        label: "Override Rate",
                        value: b.overrideRate.map { String(format: "%.0f%%", $0 * 100) } ?? "—",
                        color: (b.overrideRate ?? 0) > 0.30 ? .red : .primary
                    )
                    MiniStat(
                        label: "Overrides",
                        value: "\(b.claudeOverrides)",
                        color: b.claudeOverrides > 20 ? .orange : .primary
                    )
                }
                if let rate = b.overrideRate, rate > 0.30 {
                    Text("⚠️ Claude is overriding >30% of high-score signals — consider reducing AI gatekeeping")
                        .font(.caption).foregroundStyle(.orange)
                        .padding(.top, 4)
                }
            } else {
                Text("Baseline logging starts immediately. Signal vs Claude comparison needs ~50 signals.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - D. Slippage

    private var slippageSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Slippage Cost")
                .font(.headline)

            if let s = slippage, s.fills > 0 {
                HStack(spacing: 0) {
                    MiniStat(
                        label: "Avg Slip",
                        value: s.avgSlippagePct.map { String(format: "%.3f%%", $0) } ?? "—",
                        color: (s.avgSlippagePct ?? 0) > 0.2 ? .red : .primary
                    )
                    MiniStat(
                        label: "Total Cost",
                        value: s.totalSlippageCost.map { String(format: "$%.2f", abs($0)) } ?? "—",
                        color: .primary
                    )
                    MiniStat(
                        label: "Fills",
                        value: "\(s.fills)",
                        color: .primary
                    )
                }
                if let worst = s.worstSlippage {
                    Text("Worst fill: \(String(format: "%+.4f", worst)) per share")
                        .font(.caption2).foregroundStyle(.secondary)
                }
            } else {
                Text("Logged on every confirmed fill — data appears after first order.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - F. Time-of-Day Win Rate

    private var timeOfDaySection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Time-of-Day Win Rate")
                .font(.headline)

            if hourData.isEmpty {
                Text("Needs ~50 closed trades across different hours to be meaningful.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            } else {
                let maxWin = hourData.map(\.winRate).max() ?? 1.0
                VStack(spacing: 6) {
                    ForEach(hourData) { h in
                        HStack(spacing: 10) {
                            Text(hourLabel(h.hourEt))
                                .font(.caption).monospacedDigit()
                                .frame(width: 50, alignment: .trailing)
                            GeometryReader { geo in
                                RoundedRectangle(cornerRadius: 4)
                                    .fill(winRateColor(h.winRate))
                                    .frame(width: geo.size.width * CGFloat(maxWin > 0 ? h.winRate / maxWin : 0))
                            }
                            .frame(height: 16)
                            Text(String(format: "%.0f%%", h.winRate * 100))
                                .font(.caption).monospacedDigit()
                                .foregroundStyle(.secondary)
                                .frame(width: 36, alignment: .trailing)
                            Text("(\(h.trades))")
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - Blocked Trades

    private var blockedTradesSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Blocked Trades — Opportunity Cost")
                .font(.headline)
            Text("Which protection rule blocked the most profit?")
                .font(.caption).foregroundStyle(.secondary)

            if blockedTrades.isEmpty {
                Text("No blocked trade data yet — requires EOD price lookups to populate.")
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding()
            } else {
                ForEach(blockedTrades) { report in
                    BlockedTradeRow(report: report)
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }

    // MARK: - Load

    private func load() async {
        isLoading = true
        errorMessage = nil
        async let sc   = try? APIService.shared.getLongShortScorecard()
        async let bt   = try? APIService.shared.getBlockedTradesReport()
        async let vr   = try? APIService.shared.getPortfolioVaR()
        async let sp   = try? APIService.shared.getPerformanceBySetup()
        async let ab   = try? APIService.shared.getAIBaselineStats()
        async let sl   = try? APIService.shared.getSlippageSummary()
        async let hr   = try? APIService.shared.getPerformanceByHour()
        async let st   = try? APIService.shared.getTradingStatus()
        scorecard    = await sc ?? [:]
        blockedTrades = await bt ?? []
        varData      = await vr
        setupData    = await sp ?? []
        aiBaseline   = await ab
        slippage     = await sl
        hourData     = await hr ?? []
        tradingStatus = await st
        isLoading    = false
    }

    // MARK: - Helpers

    private func regimeColor(_ regime: String) -> Color {
        switch regime.lowercased() {
        case "bull": return .green
        case "bear": return .red
        default: return .orange
        }
    }

    private func vixColor(_ level: String) -> Color {
        switch level.lowercased() {
        case "low": return .green
        case "normal": return .primary
        case "high": return .orange
        case "extreme": return .red
        default: return .primary
        }
    }

    private func varRiskColor(_ pct: Double?) -> Color {
        guard let p = pct else { return .primary }
        if p > 10 { return .red }
        if p > 5  { return .orange }
        return .green
    }

    private func hourLabel(_ hour: Int) -> String {
        let ampm = hour < 12 ? "AM" : "PM"
        let h = hour <= 12 ? hour : hour - 12
        return "\(h)\(ampm)"
    }

    private func winRateColor(_ rate: Double) -> Color {
        if rate >= 0.6 { return .green }
        if rate >= 0.5 { return .yellow }
        return .red
    }

    private func shortTime(_ iso: String) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let d = f.date(from: iso) else { return iso }
        let df = DateFormatter()
        df.timeStyle = .short
        df.dateStyle = .none
        return df.string(from: d)
    }
}

// MARK: - Subviews

struct VarStatView: View {
    let label: String
    let dollars: Double?
    let pct: Double?
    let color: Color

    var body: some View {
        VStack(spacing: 4) {
            if let d = dollars {
                Text(String(format: "$%.0f", d))
                    .font(.title3).fontWeight(.bold).foregroundStyle(color)
            } else {
                Text("—").font(.title3).fontWeight(.bold)
            }
            if let p = pct {
                Text(String(format: "%.1f%%", p))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Text(label)
                .font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
    }
}

struct SetupTypeRow: View {
    let setup: SetupPerformance

    var plColor: Color { setup.totalPl >= 0 ? .green : .red }
    var winColor: Color { setup.winRate >= 0.55 ? .green : .orange }

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(setup.setupType.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.subheadline).fontWeight(.medium)
                Text("\(setup.trades) trades")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(String(format: "%+.1f%%", setup.avgPlPct))
                    .font(.subheadline).fontWeight(.semibold)
                    .foregroundStyle(plColor)
                Text(String(format: "%.0f%% win", setup.winRate * 100))
                    .font(.caption).foregroundStyle(winColor)
            }
        }
        .padding(10)
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

struct ScorecardCard: View {
    let side: String
    let scorecard: SideScorecard

    var sideColor: Color { side == "long" ? .green : .red }
    var sideIcon: String { side == "long" ? "arrow.up.right" : "arrow.down.right" }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Image(systemName: sideIcon).foregroundStyle(sideColor)
                Text(side.capitalized)
                    .font(.subheadline).fontWeight(.semibold).foregroundStyle(sideColor)
                Spacer()
                Text("\(scorecard.trades) trades")
                    .font(.caption).foregroundStyle(.secondary)
            }
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
                MiniStat(label: "Win Rate",
                         value: scorecard.winRate.map { String(format: "%.0f%%", $0 * 100) } ?? "—",
                         color: (scorecard.winRate ?? 0) >= 0.55 ? .green : .orange)
                MiniStat(label: "Avg Win",
                         value: scorecard.avgWinnerPct.map { String(format: "+%.1f%%", $0) } ?? "—",
                         color: .green)
                MiniStat(label: "Avg Loss",
                         value: scorecard.avgLoserPct.map { String(format: "%.1f%%", $0) } ?? "—",
                         color: .red)
                MiniStat(label: "Profit Factor",
                         value: scorecard.profitFactor.map { String(format: "%.2fx", $0) } ?? "—",
                         color: (scorecard.profitFactor ?? 0) >= 1.5 ? .green : .orange)
                MiniStat(label: "Avg Hold",
                         value: scorecard.avgHoldMins.map { formatMins($0) } ?? "—",
                         color: .primary)
            }
        }
        .padding()
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private func formatMins(_ mins: Double) -> String {
        if mins < 60 { return String(format: "%.0fm", mins) }
        return String(format: "%.1fh", mins / 60)
    }
}

struct BlockedTradeRow: View {
    let report: BlockedTradeReport

    var pnl: Double { report.totalHypotheticalPnl ?? 0 }
    var pnlColor: Color { pnl >= 0 ? .green : .red }
    var winRate: Double {
        let total = report.wouldHaveWon + report.wouldHaveLost
        return total > 0 ? Double(report.wouldHaveWon) / Double(total) : 0
    }

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text(report.blockReason.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.subheadline).fontWeight(.medium)
                HStack(spacing: 8) {
                    Text("\(report.timesBlocked) blocks").font(.caption).foregroundStyle(.secondary)
                    Text("·").foregroundStyle(.secondary)
                    Text("Would've won \(Int(winRate * 100))%").font(.caption).foregroundStyle(.secondary)
                    if let avgScore = report.avgSignalScore {
                        Text("·").foregroundStyle(.secondary)
                        Text("Avg score \(Int(avgScore))").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(pnl >= 0 ? "+\(String(format: "%.1f", pnl))%" : "\(String(format: "%.1f", pnl))%")
                    .font(.subheadline).fontWeight(.semibold).foregroundStyle(pnlColor)
                Text("missed P&L").font(.caption2).foregroundStyle(.secondary)
            }
        }
        .padding(10)
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }
}

struct MiniStat: View {
    let label: String
    let value: String
    var color: Color = .primary

    var body: some View {
        VStack(spacing: 2) {
            Text(value).font(.subheadline).fontWeight(.semibold).foregroundStyle(color)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}
