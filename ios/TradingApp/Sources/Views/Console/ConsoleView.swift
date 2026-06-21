import SwiftUI

// Backend Console — health monitor + quick data fetch + live error log stream.
// Zero AI token cost — maps taps directly to Railway API endpoints.

struct ConsoleView: View {
    @State private var health          = SystemHealth()
    @State private var logs: [BotActivity] = []
    @State private var logFilter       = LogLevel.all
    @State private var commandResult: String? = nil
    @State private var commandTitle    = ""
    @State private var isLoadingCmd    = false
    @State private var isLoadingLogs   = false
    @State private var lastRefresh: Date? = nil
    @State private var autoRefreshTask: Task<Void, Never>? = nil

    var filteredLogs: [BotActivity] {
        switch logFilter {
        case .all:    return logs
        case .errors: return logs.filter { ["circuit_breaker", "entry_rejected", "earnings_block"].contains($0.event_type) }
        case .trades: return logs.filter { ["approved", "order_placed", "trailing_stop", "scale_out"].contains($0.event_type) }
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    // ── System health ──────────────────────────────────────
                    SystemHealthGrid(health: health)
                        .cardAppear(delay: 0.0)

                    // ── Quick fetch ────────────────────────────────────────
                    QuickFetchGrid(
                        isLoading: $isLoadingCmd,
                        onCommand: runCommand
                    )
                    .cardAppear(delay: 0.06)

                    // ── Command result panel ───────────────────────────────
                    if let result = commandResult {
                        CommandResultCard(title: commandTitle, resultText: result)
                            .transition(.move(edge: .top).combined(with: .opacity))
                            .cardAppear(delay: 0)
                    }

                    // ── Log stream ─────────────────────────────────────────
                    LogStreamCard(
                        logs: filteredLogs,
                        filter: $logFilter,
                        isLoading: isLoadingLogs,
                        lastRefresh: lastRefresh,
                        onRefresh: { Task { await loadLogs() } }
                    )
                    .cardAppear(delay: 0.12)
                }
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 120)
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Image(systemName: "terminal.fill")
                            .foregroundStyle(LakshmiTheme.brandGradient)
                        Text("Console").font(.headline.weight(.bold))
                        if health.allGreen {
                            Circle().fill(LakshmiTheme.positive).frame(width: 7, height: 7)
                        }
                    }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button { Task { await loadAll() } } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.caption.weight(.semibold))
                    }
                }
            }
        }
        .task {
            await loadAll()
            startAutoRefresh()
        }
        .onDisappear { autoRefreshTask?.cancel() }
    }

    // MARK: - Loading

    private func loadAll() async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await pingHealth() }
            group.addTask { await loadLogs() }
        }
    }

    private func pingHealth() async {
        var h = SystemHealth()
        await withTaskGroup(of: Void.self) { group in
            group.addTask {
                let ok = (try? await APIService.shared.getTradingStatus()) != nil
                await MainActor.run { h.bot = ok ? .ok : .error }
            }
            group.addTask {
                let ok = (try? await APIService.shared.getAccount()) != nil
                await MainActor.run { h.account = ok ? .ok : .error }
            }
            group.addTask {
                let ok = (try? await APIService.shared.getPositions()) != nil
                await MainActor.run { h.positions = ok ? .ok : .error }
            }
            group.addTask {
                let ok = (try? await APIService.shared.getBotActivity(limit: 1)) != nil
                await MainActor.run { h.activityLog = ok ? .ok : .error }
            }
        }
        await MainActor.run { health = h }
    }

    private func loadLogs() async {
        await MainActor.run { isLoadingLogs = true }
        let events = (try? await APIService.shared.getBotActivity(limit: 60)) ?? []
        await MainActor.run {
            logs = events
            isLoadingLogs = false
            lastRefresh = Date()
        }
    }

    private func startAutoRefresh() {
        autoRefreshTask?.cancel()
        autoRefreshTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)  // 30s
                guard !Task.isCancelled else { break }
                await loadLogs()
            }
        }
    }

    // MARK: - Quick fetch commands

    private func runCommand(_ cmd: QuickCommand) {
        guard !isLoadingCmd else { return }
        isLoadingCmd = true
        commandTitle = cmd.label
        commandResult = nil
        Task {
            let result = await fetchCommand(cmd)
            await MainActor.run {
                withAnimation(LakshmiTheme.springSnappy) { commandResult = result }
                isLoadingCmd = false
            }
        }
    }

    private func fetchCommand(_ cmd: QuickCommand) async -> String {
        switch cmd {
        case .status:
            guard let s = try? await APIService.shared.getTradingStatus() else { return "❌ Failed to fetch status" }
            return """
            Running:     \(s.isRunning ? "✅ YES" : "⏸ NO")
            Regime:      \(s.brainRegime?.uppercased() ?? "—")
            VIX Level:   \(s.vixLevel ?? "—")
            Capital Mult: \(s.regimeCapitalMult.map { String(format: "%.2f×", $0) } ?? "—")
            Confidence:  \(s.regimeConfidence.map { String(format: "%.0f%%", $0 * 100) } ?? "—")
            """

        case .account:
            guard let a = try? await APIService.shared.getAccount() else { return "❌ Failed to fetch account" }
            let sign = a.dayPl >= 0 ? "+" : ""
            return """
            Portfolio:   $\(Int(a.portfolioValue).formatted())
            Cash:        $\(Int(a.cash).formatted())
            Buying Power: $\(Int(a.buyingPower).formatted())
            Day P&L:     \(sign)$\(String(format: "%.2f", a.dayPl)) (\(String(format: "%+.2f", a.dayPlPercent))%)
            """

        case .positions:
            guard let positions = try? await APIService.shared.getPositions() else { return "❌ Failed to fetch positions" }
            if positions.isEmpty { return "No open positions" }
            return positions.map { p in
                let sign = p.unrealizedPl >= 0 ? "+" : ""
                return "\(p.symbol.padding(toLength: 6, withPad: " ", startingAt: 0)) $\(String(format: "%.2f", p.currentPrice))  \(sign)$\(String(format: "%.0f", p.unrealizedPl)) (\(String(format: "%+.1f", p.unrealizedPlPercent))%)"
            }.joined(separator: "\n")

        case .lastAI:
            guard let decisions = try? await APIService.shared.getRecentAIDecisions(limit: 1),
                  let d = decisions.first else { return "No AI decisions yet" }
            return """
            Symbol:  \(d.symbol ?? "—")
            Action:  \(d.event_type.uppercased())
            Time:    \(d.timestamp ?? "—")
            Message: \(d.message)
            """

        case .recentTrades:
            guard let trades = try? await APIService.shared.getPositionHistory(limit: 8) else { return "❌ Failed to fetch trades" }
            if trades.isEmpty { return "No closed trades yet" }
            return trades.map { t in
                let pl = t.realized_pl ?? 0
                let sign = pl >= 0 ? "+" : ""
                return "\(t.symbol.padding(toLength: 6, withPad: " ", startingAt: 0)) \((t.side ?? "long").uppercased())  \(sign)$\(String(format: "%.0f", pl))  \(t.exit_reason ?? "")"
            }.joined(separator: "\n")

        case .errors:
            guard let events = try? await APIService.shared.getBotActivity(limit: 60) else { return "❌ Failed to fetch activity" }
            let errorTypes = ["circuit_breaker", "entry_rejected", "earnings_block"]
            let errors = events.filter { errorTypes.contains($0.event_type) }
            if errors.isEmpty { return "✅ No failures in recent activity" }
            return errors.prefix(15).map { e in
                "[\(e.event_type.uppercased())] \(e.symbol.map { $0 + " " } ?? "")\(e.message)"
            }.joined(separator: "\n")

        case .performance:
            guard let perf = try? await APIService.shared.getPerformanceSummary() else { return "❌ Failed to fetch performance" }
            let totalPL = perf.total_realized_pl
            let sign = totalPL >= 0 ? "+" : ""
            return """
            Win Rate:    \(String(format: "%.1f%%", perf.win_rate_pct))
            Total P&L:   \(sign)$\(String(format: "%.0f", totalPL))
            Trades:      \(perf.total_trades) (\(perf.wins)W / \(perf.losses)L)
            Avg Win:     +\(String(format: "%.1f%%", perf.avg_win_pct))
            Avg Loss:    \(String(format: "%.1f%%", perf.avg_loss_pct))
            """
        }
    }
}

// MARK: - System health

struct SystemHealth {
    var bot         = HealthState.checking
    var account     = HealthState.checking
    var positions   = HealthState.checking
    var activityLog = HealthState.checking

    var allGreen: Bool { [bot, account, positions, activityLog].allSatisfy { $0 == .ok } }

    enum HealthState: Equatable { case checking, ok, error }
}

private struct SystemHealthGrid: View {
    let health: SystemHealth

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("SYSTEM HEALTH")
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)

            HStack(spacing: 8) {
                HealthDot(label: "Bot",      state: health.bot)
                HealthDot(label: "Account",  state: health.account)
                HealthDot(label: "Positions", state: health.positions)
                HealthDot(label: "Log DB",   state: health.activityLog)
            }
        }
        .padding(14)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
    }
}

private struct HealthDot: View {
    let label: String
    let state: SystemHealth.HealthState
    @State private var pulse = false

    private var color: Color {
        switch state {
        case .checking: return .orange
        case .ok:       return LakshmiTheme.positive
        case .error:    return LakshmiTheme.negative
        }
    }

    var body: some View {
        VStack(spacing: 5) {
            ZStack {
                if state == .ok {
                    Circle().fill(color.opacity(0.25)).frame(width: 22, height: 22)
                        .scaleEffect(pulse ? 1.6 : 1).opacity(pulse ? 0 : 0.6)
                        .animation(.easeOut(duration: 1.4).repeatForever(autoreverses: false), value: pulse)
                }
                Circle().fill(color).frame(width: 10, height: 10)
            }
            .frame(width: 22, height: 22)
            .onAppear { if state == .ok { pulse = true } }

            Text(label).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Quick fetch grid

enum QuickCommand: CaseIterable {
    case status, account, positions, lastAI, recentTrades, errors, performance

    var label: String {
        switch self {
        case .status:       return "Bot Status"
        case .account:      return "Account"
        case .positions:    return "Positions"
        case .lastAI:       return "Last AI"
        case .recentTrades: return "Trades"
        case .errors:       return "Errors"
        case .performance:  return "Performance"
        }
    }

    var icon: String {
        switch self {
        case .status:       return "brain"
        case .account:      return "dollarsign.circle"
        case .positions:    return "chart.pie"
        case .lastAI:       return "sparkles"
        case .recentTrades: return "list.bullet"
        case .errors:       return "exclamationmark.triangle"
        case .performance:  return "chart.line.uptrend.xyaxis"
        }
    }

    var color: Color {
        switch self {
        case .status:       return LakshmiTheme.purple
        case .account:      return LakshmiTheme.positive
        case .positions:    return LakshmiTheme.blue
        case .lastAI:       return .indigo
        case .recentTrades: return .teal
        case .errors:       return LakshmiTheme.negative
        case .performance:  return .orange
        }
    }
}

private struct QuickFetchGrid: View {
    @Binding var isLoading: Bool
    let onCommand: (QuickCommand) -> Void

    let cols = [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("QUICK FETCH")
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)

            LazyVGrid(columns: cols, spacing: 8) {
                ForEach(QuickCommand.allCases, id: \.self) { cmd in
                    Button { onCommand(cmd) } label: {
                        VStack(spacing: 5) {
                            ZStack {
                                RoundedRectangle(cornerRadius: 8)
                                    .fill(cmd.color.opacity(0.12))
                                    .frame(width: 36, height: 36)
                                if isLoading {
                                    ProgressView().scaleEffect(0.6)
                                } else {
                                    Image(systemName: cmd.icon)
                                        .font(.system(size: 15, weight: .semibold))
                                        .foregroundStyle(cmd.color)
                                }
                            }
                            Text(cmd.label)
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .minimumScaleFactor(0.8)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 10)
                        .background(LakshmiTheme.card)
                        .clipShape(RoundedRectangle(cornerRadius: 10))
                    }
                    .buttonStyle(PressScaleButtonStyle(scale: 0.93))
                    .disabled(isLoading)
                }
            }
        }
        .padding(14)
        .background(Color(.tertiarySystemBackground))
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
    }
}

// MARK: - Command result

private struct CommandResultCard: View {
    let title: String
    let resultText: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: "terminal").font(.caption).foregroundStyle(LakshmiTheme.positive)
                Text(title.uppercased()).font(.caption.weight(.bold)).foregroundStyle(.secondary)
                Spacer()
                Text("RESULT").font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(LakshmiTheme.positive)
            }
            Text(resultText)
                .font(.system(size: 12, weight: .regular, design: .monospaced))
                .foregroundStyle(.primary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm)
                .fill(LakshmiTheme.positive.opacity(0.06))
                .overlay(
                    RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm)
                        .stroke(LakshmiTheme.positive.opacity(0.2), lineWidth: 1)
                )
        )
    }
}

// MARK: - Log stream

enum LogLevel { case all, errors, trades }

private struct LogStreamCard: View {
    let logs: [BotActivity]
    @Binding var filter: LogLevel
    let isLoading: Bool
    let lastRefresh: Date?
    let onRefresh: () -> Void
    @Namespace private var logNS

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Header row
            HStack {
                Text("LOG STREAM")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.secondary)
                if isLoading { ProgressView().scaleEffect(0.6) }
                Spacer()
                if let r = lastRefresh {
                    HStack(spacing: 3) {
                        Circle().fill(LakshmiTheme.positive).frame(width: 5, height: 5)
                        Text("Updated \(r, style: .relative)")
                    }
                    .font(.system(size: 9)).foregroundStyle(.secondary)
                }
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise").font(.caption2).foregroundStyle(.secondary)
                }
            }

            // Filter tabs
            HStack(spacing: 4) {
                ForEach([(LogLevel.all, "All"), (.errors, "Errors"), (.trades, "Trades")], id: \.1) { level, label in
                    Button {
                        withAnimation(LakshmiTheme.springSnappy) { filter = level }
                    } label: {
                        Text(label)
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .foregroundStyle(filter == level ? .white : .secondary)
                            .background {
                                if filter == level {
                                    Capsule().fill(LakshmiTheme.brandGradient)
                                        .matchedGeometryEffect(id: "logFilter", in: logNS)
                                }
                            }
                    }
                    .buttonStyle(PressScaleButtonStyle(scale: 0.94))
                }
            }

            Divider()

            if logs.isEmpty {
                HStack {
                    Spacer()
                    Text(isLoading ? "Loading…" : "No events")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                }
                .padding(.vertical, 20)
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(logs.prefix(40).enumerated()), id: \.element.id) { idx, event in
                        LogEventRow(event: event)
                        if idx < min(logs.count - 1, 39) {
                            Divider().padding(.leading, 48)
                        }
                    }
                }
            }
        }
        .padding(14)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
    }
}

private struct LogEventRow: View {
    let event: BotActivity

    private var severity: Color {
        switch event.event_type {
        case "circuit_breaker":              return LakshmiTheme.negative
        case "entry_rejected", "earnings_block": return .orange
        case "approved", "order_placed":    return LakshmiTheme.positive
        case "trailing_stop", "scale_out":  return LakshmiTheme.blue
        default:                             return .secondary
        }
    }

    private var icon: String {
        switch event.event_type {
        case "circuit_breaker":  return "exclamationmark.octagon.fill"
        case "entry_rejected":   return "xmark.circle.fill"
        case "earnings_block":   return "calendar.badge.exclamationmark"
        case "approved":         return "checkmark.circle.fill"
        case "order_placed":     return "arrow.up.circle.fill"
        case "trailing_stop":    return "arrow.uturn.down.circle.fill"
        case "scale_out":        return "minus.circle.fill"
        default:                 return "circle.fill"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .foregroundStyle(severity)
                .font(.system(size: 16))
                .frame(width: 28)
                .padding(.top, 1)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    if let sym = event.symbol {
                        Text(sym).font(.caption.weight(.bold))
                    }
                    Text(event.event_type.replacingOccurrences(of: "_", with: " ").uppercased())
                        .font(.system(size: 8, weight: .bold, design: .monospaced))
                        .foregroundStyle(severity)
                    Spacer()
                    if let ts = event.timestamp {
                        Text(relativeTime(ts))
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                }
                Text(event.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
        .padding(.vertical, 8)
    }

    private func relativeTime(_ iso: String) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let d = f.date(from: iso) else { return "—" }
        let mins = Int(-d.timeIntervalSinceNow / 60)
        if mins < 1 { return "now" }
        if mins < 60 { return "\(mins)m ago" }
        return "\(mins / 60)h ago"
    }
}
