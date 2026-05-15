import SwiftUI

struct AIView: View {
    @StateObject private var vm = TradingViewModel()
    @State private var circuitBreakerDayPl: Double? = nil
    @State private var circuitBreakerLimit: Double = 3.0
    @State private var cycleIntervalSeconds: Int = 600  // default 10 min, refreshed on load
    @State private var recentDecisions: [BotActivity] = []
    @State private var decisionsExpanded: Bool = false

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.status == nil {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.status == nil {
                    ErrorView(message: error) { await vm.load() }
                } else {
                    ScrollView {
                        VStack(spacing: 16) {
                            // ── Circuit breaker banner (only shown when active) ──
                            if let dayPl = circuitBreakerDayPl, dayPl < -circuitBreakerLimit {
                                CircuitBreakerBanner(dayPlPercent: dayPl, limitPct: circuitBreakerLimit)
                            }

                            // ════════════════════════════════
                            // INFO — What the bot is doing
                            // ════════════════════════════════

                            PreMarketView()

                            // ── Recent AI Decisions ──
                            RecentAIDecisionsCard(
                                decisions: recentDecisions,
                                cycleIntervalSeconds: cycleIntervalSeconds,
                                isExpanded: $decisionsExpanded
                            )

                            // ── Active short positions ──
                            OpenShortsCard()

                            // ── Real-time bot activity log ──
                            BotActivityView()

                            // ── Reports & History ──
                            HStack(spacing: 12) {
                                NavigationLink {
                                    TradeHistoryView()
                                } label: {
                                    Label("Trade History", systemImage: "chart.bar.xaxis")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.bordered)

                                NavigationLink {
                                    PerformanceView()
                                        .navigationTitle("Performance")
                                } label: {
                                    Label("Performance", systemImage: "chart.line.uptrend.xyaxis")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.borderedProminent)
                            }

                            HStack(spacing: 12) {
                                NavigationLink {
                                    TaxSummaryView()
                                } label: {
                                    Label("Est. Taxes", systemImage: "dollarsign.circle")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.bordered)

                                NavigationLink {
                                    ProfitReserveView()
                                } label: {
                                    Label("Profit Reserve", systemImage: "banknote")
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.bordered)
                                .tint(.green)
                            }

                            NavigationLink {
                                EODReportView()
                            } label: {
                                HStack {
                                    Image(systemName: "chart.bar.doc.horizontal")
                                    Text("Daily Report")
                                        .fontWeight(.semibold)
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                }
                                .padding()
                                .background(Color(.secondarySystemBackground))
                                .cornerRadius(14)
                            }
                            .foregroundColor(.primary)

                            // ════════════════════════════════
                            // SETTINGS — Bot configuration
                            // ════════════════════════════════

                            BotControlView(vm: vm)

                            StrategyPickerView()

                            TradingFloorView()

                            WatchlistEditorView()

                            TradingBudgetCard()

                            RiskSettingsCard()

                            // ── Prompt Viewer / Override ──
                            NavigationLink {
                                PromptViewerView()
                            } label: {
                                HStack {
                                    ZStack {
                                        Circle().fill(Color.purple.opacity(0.12)).frame(width: 36, height: 36)
                                        Image(systemName: "text.badge.plus")
                                            .foregroundStyle(.purple).font(.system(size: 15))
                                    }
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text("Claude Prompt")
                                            .font(.subheadline).fontWeight(.semibold)
                                        Text("View last prompt · set override instructions")
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                .padding()
                                .background(Color(.systemGray6))
                                .cornerRadius(16)
                            }
                            .foregroundColor(.primary)
                        }
                        .padding()
                    }
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("AI Agent")
        }
        .task {
            await vm.load()
            await loadCircuitBreakerStatus()
            if let settings = try? await APIService.shared.getRiskSettings() {
                cycleIntervalSeconds = settings.cycle_interval_seconds
            }
            await loadRecentDecisions()
        }
        .onReceive(NotificationCenter.default.publisher(for: .circuitBreakerFired)) { note in
            if let dayPl = note.userInfo?["day_pl_percent"] as? Double {
                circuitBreakerDayPl = dayPl
            }
        }
    }

    private func loadCircuitBreakerStatus() async {
        // Check account P&L and risk settings to set circuit breaker state
        if let riskSettings = try? await APIService.shared.getRiskSettings() {
            circuitBreakerLimit = riskSettings.daily_loss_limit_pct
        }
        // Account P&L comes from the dashboard VM; rely on WebSocket for live updates
    }

    private func loadRecentDecisions() async {
        if let decisions = try? await APIService.shared.getRecentAIDecisions(limit: 20) {
            recentDecisions = decisions
        }
    }
}

extension Notification.Name {
    static let circuitBreakerFired = Notification.Name("circuitBreakerFired")
}

// MARK: - Recent AI Decisions Card

struct RecentAIDecisionsCard: View {
    let decisions: [BotActivity]
    let cycleIntervalSeconds: Int
    @Binding var isExpanded: Bool

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private func parseDate(_ raw: String?) -> Date? {
        guard let raw else { return nil }
        return Self.isoFormatter.date(from: raw)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // ── Header ──
            HStack(spacing: 10) {
                ZStack {
                    Circle().fill(Color.purple.opacity(0.12)).frame(width: 36, height: 36)
                    Image(systemName: "brain.head.profile")
                        .foregroundStyle(.purple).font(.system(size: 16))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Recent AI Decisions")
                        .font(.headline).foregroundStyle(.primary)
                    Text("\(decisions.count) decisions")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                // Next cycle countdown from the most recent decision
                if let latest = decisions.first, let ts = parseDate(latest.timestamp) {
                    let nextRun = ts.addingTimeInterval(TimeInterval(cycleIntervalSeconds))
                    if nextRun > Date() {
                        HStack(spacing: 3) {
                            Image(systemName: "clock.arrow.circlepath").font(.caption2)
                            Text("Next in ") + Text(nextRun, style: .relative)
                        }
                        .font(.caption2).foregroundStyle(.blue)
                    }
                }
                Button(action: { withAnimation { isExpanded.toggle() } }) {
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding()

            if decisions.isEmpty {
                Text("No AI decisions yet — waiting for first cycle.")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding([.horizontal, .bottom])
            } else {
                Divider().padding(.horizontal)

                // Always show the most recent decision in full
                if let latest = decisions.first {
                    AIDecisionRow(event: latest, showFullMessage: true)
                }

                // Show the rest in compact form if expanded
                if isExpanded && decisions.count > 1 {
                    ForEach(decisions.dropFirst()) { event in
                        Divider().padding(.leading, 52)
                        AIDecisionRow(event: event, showFullMessage: false)
                    }
                }

                if decisions.count > 1 {
                    Button(action: { withAnimation { isExpanded.toggle() } }) {
                        Text(isExpanded ? "Show less" : "Show \(decisions.count - 1) more decisions")
                            .font(.caption).foregroundStyle(.blue)
                    }
                    .padding(.horizontal, 16).padding(.bottom, 12)
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }
}

struct AIDecisionRow: View {
    let event: BotActivity
    let showFullMessage: Bool

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private var parsedDate: Date? {
        guard let raw = event.timestamp else { return nil }
        return Self.isoFormatter.date(from: raw)
    }

    private var actionBadgeInfo: (text: String, color: Color)? {
        switch event.event_type {
        case "approved":
            // Extract action from message: "BUY TQQQ x412..." or "SELL ..."
            let upper = event.message.uppercased()
            if upper.hasPrefix("BUY ")   { return ("BUY",   .green) }
            if upper.hasPrefix("SELL ")  { return ("SELL",  .red) }
            if upper.hasPrefix("SHORT ") { return ("SHORT", .orange) }
            if upper.hasPrefix("COVER ") { return ("COVER", .teal) }
            return ("TRADE", .green)
        case "entry_rejected":  return ("REJECTED", .orange)
        case "earnings_block":  return ("BLOCKED",  .yellow)
        case "circuit_breaker": return ("HALTED",   .red)
        default: return nil
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            // Status icon
            Image(systemName: event.eventIcon)
                .foregroundStyle(Color(event.eventColor == "green" ? .systemGreen :
                                       event.eventColor == "orange" ? .systemOrange :
                                       event.eventColor == "red" ? .systemRed : .systemGray))
                .font(.system(size: 20))
                .frame(width: 28)
                .padding(.top, 2)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    if let badge = actionBadgeInfo {
                        Text(badge.text)
                            .font(.caption2).fontWeight(.bold)
                            .foregroundStyle(.white)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(badge.color)
                            .clipShape(Capsule())
                    }
                    if let symbol = event.symbol {
                        Text(symbol).font(.subheadline).fontWeight(.semibold)
                    }
                    Spacer()
                    if let date = parsedDate {
                        Text(date, style: .relative)
                            .font(.caption2).foregroundStyle(.tertiary)
                    }
                }
                Text(event.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(showFullMessage ? nil : 2)
                    .lineSpacing(2)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }
}

// MARK: - Risk Settings Card

struct RiskSettingsCard: View {
    @State private var settings: RiskSettings = .defaults
    @State private var isLoading = true
    @State private var isSaving = false
    @State private var saveError: String? = nil
    @State private var saveSuccess = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack(spacing: 10) {
                ZStack {
                    Circle().fill(Color.blue.opacity(0.12)).frame(width: 36, height: 36)
                    Image(systemName: "slider.horizontal.3")
                        .foregroundStyle(.blue).font(.system(size: 16))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("Risk Controls")
                        .font(.headline).foregroundStyle(.primary)
                    Text("Saved to cloud · takes effect next cycle")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if isSaving {
                    ProgressView().scaleEffect(0.8)
                } else if saveSuccess {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .transition(.scale.combined(with: .opacity))
                }
            }
            .padding()

            Divider().padding(.horizontal)

            if isLoading {
                ProgressView().frame(maxWidth: .infinity).padding()
            } else {
                VStack(spacing: 0) {
                    // ── New: Trade Throttle ──
                    RiskSectionHeader(title: "TRADE THROTTLE")

                    RiskStepperRow(
                        label: "Bot Cycle Interval",
                        hint: "How often the bot scans and trades. 5 min catches faster moves; 10 min is default.",
                        value: Binding(
                            get: { settings.cycle_interval_seconds / 60 },
                            set: { settings.cycle_interval_seconds = $0 * 60 }
                        ),
                        range: 1...30,
                        unit: "min"
                    ) { saveSettings() }

                    Divider().padding(.leading, 16)

                    RiskStepperRow(
                        label: "Max Trades Per Cycle",
                        hint: "Limits new buys/shorts per cycle. Sells & covers always go through.",
                        value: $settings.max_trades_per_cycle,
                        range: 1...10,
                        unit: "trades"
                    ) { saveSettings() }

                    Divider().padding(.leading, 16)

                    RiskPercentSliderRow(
                        label: "Penny Stock Position Cap",
                        hint: "Max portfolio % for stocks under $5. Prevents 7,000-share PLUG situations.",
                        value: $settings.max_penny_position_pct,
                        range: 1.0...15.0,
                        step: 0.5,
                        unit: "%"
                    ) { saveSettings() }

                    // ── Loss Protection ──
                    RiskSectionHeader(title: "LOSS PROTECTION")

                    RiskPercentSliderRow(
                        label: "Daily Loss Circuit Breaker",
                        hint: "Blocks new buys/shorts for the day if portfolio drops this much. Sells still work.",
                        value: $settings.daily_loss_limit_pct,
                        range: 1.0...15.0,
                        step: 0.5,
                        unit: "%"
                    ) { saveSettings() }

                    Divider().padding(.leading, 16)

                    RiskPercentSliderRow(
                        label: "Stop Loss",
                        hint: "Trailing stop fallback. Claude sets per-trade stops; this is the floor.",
                        value: Binding(
                            get: { settings.stop_loss_pct * 100 },
                            set: { settings.stop_loss_pct = $0 / 100 }
                        ),
                        range: 1.0...20.0,
                        step: 0.5,
                        unit: "%"
                    ) { saveSettings() }

                    Divider().padding(.leading, 16)

                    RiskPercentSliderRow(
                        label: "Take Profit",
                        hint: "Profit target fallback. Claude overrides this per trade with a tighter target.",
                        value: Binding(
                            get: { settings.take_profit_pct * 100 },
                            set: { settings.take_profit_pct = $0 / 100 }
                        ),
                        range: 5.0...50.0,
                        step: 1.0,
                        unit: "%"
                    ) { saveSettings() }


                }

                if let err = saveError {
                    Text(err)
                        .font(.caption).foregroundStyle(.red)
                        .padding(.horizontal).padding(.bottom, 8)
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .task { await loadSettings() }
    }

    private func loadSettings() async {
        isLoading = true
        if let loaded = try? await APIService.shared.getRiskSettings() {
            settings = loaded
        }
        isLoading = false
    }

    private func saveSettings() {
        guard !isSaving else { return }
        isSaving = true
        saveError = nil
        saveSuccess = false
        let snapshot = settings
        Task {
            do {
                try await APIService.shared.setRiskSettings(snapshot)
                await MainActor.run {
                    isSaving = false
                    saveSuccess = true
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run { saveSuccess = false }
            } catch {
                await MainActor.run {
                    isSaving = false
                    saveError = "Save failed: \(error.localizedDescription)"
                }
            }
        }
    }
}

// MARK: - Risk Settings Subviews

private struct RiskSectionHeader: View {
    let title: String
    var body: some View {
        Text(title)
            .font(.caption2).fontWeight(.semibold)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 4)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct RiskStepperRow: View {
    let label: String
    let hint: String
    @Binding var value: Int
    let range: ClosedRange<Int>
    let unit: String
    let onChange: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(label).font(.subheadline).fontWeight(.medium)
                    Text(hint).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
                HStack(spacing: 8) {
                    Text("\(value) \(unit)")
                        .font(.subheadline).fontWeight(.semibold)
                        .foregroundStyle(.blue)
                        .monospacedDigit()
                        .frame(minWidth: 60, alignment: .trailing)
                    Stepper("", value: $value, in: range,
                            onEditingChanged: { editing in if !editing { onChange() } })
                        .labelsHidden()
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

private struct RiskPercentSliderRow: View {
    let label: String
    let hint: String
    @Binding var value: Double
    let range: ClosedRange<Double>
    let step: Double
    let unit: String
    let onChange: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(label).font(.subheadline).fontWeight(.medium)
                Spacer()
                Text(String(format: value.truncatingRemainder(dividingBy: 1) == 0 ? "%.0f%@" : "%.1f%@", value, unit))
                    .font(.subheadline).fontWeight(.semibold)
                    .foregroundStyle(.blue)
                    .monospacedDigit()
            }
            Slider(value: $value, in: range, step: step) { editing in
                if !editing { onChange() }
            }
            Text(hint).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

// MARK: - Open Shorts Card

struct OpenShortsCard: View {
    @State private var shorts: [Position] = []
    @State private var isLoading = true

    var body: some View {
        Group {
            if !isLoading && !shorts.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    // Header
                    HStack(spacing: 10) {
                        ZStack {
                            Circle().fill(Color.orange.opacity(0.15)).frame(width: 36, height: 36)
                            Image(systemName: "arrow.down.circle.fill")
                                .foregroundStyle(.orange).font(.system(size: 16))
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Active Short Positions")
                                .font(.headline).foregroundStyle(.primary)
                            Text("\(shorts.count) open short\(shorts.count == 1 ? "" : "s")")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        // Total unrealized P&L across all shorts
                        let totalPl = shorts.reduce(0) { $0 + $1.unrealizedPl }
                        Text("\(totalPl >= 0 ? "+" : "")$\(String(format: "%.0f", totalPl))")
                            .font(.subheadline).fontWeight(.bold)
                            .foregroundStyle(totalPl >= 0 ? .green : .red)
                    }
                    .padding()

                    Divider().padding(.horizontal)

                    ForEach(shorts) { position in
                        ShortPositionRow(position: position)
                        if position.id != shorts.last?.id {
                            Divider().padding(.leading, 52)
                        }
                    }
                    .padding(.bottom, 8)
                }
                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        let positions = (try? await APIService.shared.getPositions()) ?? []
        shorts = positions.filter { $0.isShort }
        isLoading = false
    }
}

struct ShortPositionRow: View {
    let position: Position

    var body: some View {
        HStack(spacing: 12) {
            // Direction indicator
            RoundedRectangle(cornerRadius: 3)
                .fill(Color.orange)
                .frame(width: 4)
                .padding(.vertical, 8)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(position.symbol)
                        .font(.subheadline).fontWeight(.semibold)
                    Text("SHORT")
                        .font(.caption2).fontWeight(.bold)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color.orange)
                        .clipShape(Capsule())
                    Spacer()
                    // P&L %
                    Text("\(position.unrealizedPlPercent >= 0 ? "+" : "")\(String(format: "%.1f", position.unrealizedPlPercent))%")
                        .font(.subheadline).fontWeight(.bold)
                        .foregroundStyle(position.unrealizedPlPercent >= 0 ? .green : .red)
                }
                HStack(spacing: 8) {
                    Text("Entry $\(String(format: "%.2f", position.avgEntryPrice)) → Now $\(String(format: "%.2f", position.currentPrice))")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Text("\(Int(position.qty)) shares")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}
