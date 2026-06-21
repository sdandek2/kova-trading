import SwiftUI

// Lakshmi tab — live status hero, decision feed, quick bot toggle.
// Settings (strategy, risk, watchlist) live in the More tab.

struct BotTabView: View {
    @StateObject private var vm          = TradingViewModel()
    @State private var decisions: [BotActivity] = []
    @State private var cycleSeconds      = 600
    @State private var circuitLimit      = 4.0
    @State private var settingsExpanded  = false
    @Namespace private var heroNS
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    // ── Hero status card ───────────────────────────────────
                    BotHeroCard(vm: vm)
                        .cardAppear(delay: 0.0)

                    // ── Recent Decisions ──────────────────────────────────
                    DecisionFeedCard(decisions: decisions, cycleSeconds: cycleSeconds)
                        .cardAppear(delay: 0.07)

                    // ── Short positions (when any open) ──────────────────
                    OpenShortsCard()
                        .cardAppear(delay: 0.12)

                    // ── Bot activity log ──────────────────────────────────
                    BotActivityView()
                        .cardAppear(delay: 0.16)

                    // ── Pre-market brief ──────────────────────────────────
                    PreMarketView()
                        .cardAppear(delay: 0.20)

                    // ── Quick settings row ─────────────────────────────────
                    NavigationLink {
                        BotSettingsPage()
                    } label: {
                        GlassNavRow(
                            icon: "gearshape.2.fill",
                            title: "Bot Configuration",
                            subtitle: "Strategy · risk · watchlist · model",
                            color: LakshmiTheme.purple
                        )
                    }
                    .buttonStyle(PressScaleButtonStyle())
                    .cardAppear(delay: 0.24)

                    // ── Reports row ────────────────────────────────────────
                    HStack(spacing: 12) {
                        NavigationLink { TradeHistoryView() } label: {
                            GlassNavRow(icon: "chart.bar.xaxis", title: "Trade History",
                                        subtitle: "All trades", color: LakshmiTheme.blue)
                        }
                        .buttonStyle(PressScaleButtonStyle())

                        NavigationLink { PerformanceView().navigationTitle("Performance") } label: {
                            GlassNavRow(icon: "chart.line.uptrend.xyaxis", title: "Performance",
                                        subtitle: "Charts", color: LakshmiTheme.positive)
                        }
                        .buttonStyle(PressScaleButtonStyle())
                    }
                    .cardAppear(delay: 0.28)
                }
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 120)
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button {
                        dismiss()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 20))
                            .foregroundStyle(.secondary)
                    }
                }
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        Text("🪷").font(.system(size: 16))
                        Text("Lakshmi").font(.headline.weight(.bold))
                        if vm.status?.isRunning == true {
                            LiveDot(color: LakshmiTheme.positive)
                        }
                    }
                }
                ToolbarItem(placement: .primaryAction) {
                    Toggle("", isOn: Binding(
                        get: { vm.status?.isRunning ?? false },
                        set: { _ in Task { await vm.toggleBot() } }
                    ))
                    .labelsHidden()
                    .tint(LakshmiTheme.positive)
                }
            }
            .refreshable { await refresh() }
        }
        .task { await refresh() }
    }

    private func refresh() async {
        await vm.load()
        decisions = (try? await APIService.shared.getRecentAIDecisions(limit: 25)) ?? []
        if let r = try? await APIService.shared.getRiskSettings() {
            cycleSeconds = r.cycle_interval_seconds
            circuitLimit = r.daily_loss_limit_pct
        }
    }
}

// MARK: - Hero status card

private struct BotHeroCard: View {
    @ObservedObject var vm: TradingViewModel
    @State private var orbMove = false

    private var regime: String  { vm.status?.brainRegime ?? "—" }
    private var running: Bool   { vm.status?.isRunning ?? false }
    private var mult: Double    { vm.status?.regimeCapitalMult ?? 1.0 }
    private var vix: String     { vm.status?.vixLevel ?? "—" }
    private var conf: Double    { vm.status?.regimeConfidence ?? 0 }

    private var regimeColor: Color {
        switch regime.lowercased() {
        case "bull":  return LakshmiTheme.positive
        case "bear":  return LakshmiTheme.negative
        case "chop":  return .orange
        default:      return .secondary
        }
    }

    private var regimeEmoji: String {
        switch regime.lowercased() {
        case "bull": return "🐂"
        case "bear": return "🐻"
        case "chop": return "🌊"
        default:     return "🔍"
        }
    }

    var body: some View {
        ZStack {
            // Ambient orbs
            Circle()
                .fill(regimeColor.opacity(0.18))
                .frame(width: 200, height: 200)
                .blur(radius: 60)
                .offset(x: orbMove ? 50 : -50, y: orbMove ? -20 : 20)
                .animation(.easeInOut(duration: 6).repeatForever(autoreverses: true), value: orbMove)

            Circle()
                .fill(LakshmiTheme.purple.opacity(0.14))
                .frame(width: 160, height: 160)
                .blur(radius: 50)
                .offset(x: orbMove ? -60 : 40, y: orbMove ? 40 : -30)
                .animation(.easeInOut(duration: 8).repeatForever(autoreverses: true).delay(1.5), value: orbMove)

            // Content
            VStack(spacing: 14) {
                // Status row
                HStack {
                    HStack(spacing: 6) {
                        if running {
                            LiveDot(color: LakshmiTheme.positive)
                            Text("RUNNING")
                                .font(.caption.weight(.bold))
                                .foregroundStyle(LakshmiTheme.positive)
                        } else {
                            Circle().fill(Color.secondary).frame(width: 8, height: 8)
                            Text("STOPPED")
                                .font(.caption.weight(.bold))
                                .foregroundStyle(.secondary)
                        }
                    }
                    Spacer()
                    HStack(spacing: 4) {
                        Text(regimeEmoji)
                        Text(regime.uppercased())
                            .font(.caption.weight(.bold))
                            .foregroundStyle(regimeColor)
                    }
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(regimeColor.opacity(0.12))
                    .clipShape(Capsule())
                }

                // Countdown
                if running, vm.countdown > 0 {
                    HStack {
                        Image(systemName: "clock.arrow.circlepath").font(.caption)
                        Text("Next cycle in ") + Text("\(vm.countdown)s").bold()
                    }
                    .font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                Divider().opacity(0.4)

                // Stats grid
                HStack(spacing: 0) {
                    BotStatCell(label: "Capital Mult", value: String(format: "%.1f×", mult), color: mult > 1 ? LakshmiTheme.positive : (mult < 1 ? .orange : .primary))
                    Divider().frame(height: 36)
                    BotStatCell(label: "VIX", value: vix.capitalized, color: vix == "extreme" ? LakshmiTheme.negative : .primary)
                    Divider().frame(height: 36)
                    BotStatCell(label: "Confidence", value: String(format: "%.0f%%", conf * 100), color: conf > 0.75 ? LakshmiTheme.positive : .primary)
                }
            }
            .padding(18)
        }
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
        .onAppear { orbMove = true }
    }
}

private struct BotStatCell: View {
    let label: String
    let value: String
    let color: Color

    var body: some View {
        VStack(spacing: 3) {
            Text(value).font(.subheadline.weight(.bold)).foregroundStyle(color)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Decision feed card

private struct DecisionFeedCard: View {
    let decisions: [BotActivity]
    let cycleSeconds: Int
    @State private var expanded = false

    private static let isoFmt: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack(spacing: 10) {
                ZStack {
                    Circle().fill(LakshmiTheme.purple.opacity(0.12)).frame(width: 36, height: 36)
                    Image(systemName: "brain.head.profile").foregroundStyle(LakshmiTheme.purple).font(.system(size: 16))
                }
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text("AI Decisions").font(.headline)
                        if !decisions.isEmpty { LiveDot(color: LakshmiTheme.purple) }
                    }
                    // Next cycle
                    if let latest = decisions.first,
                       let ts = Self.isoFmt.date(from: latest.timestamp ?? ""),
                       ts.addingTimeInterval(TimeInterval(cycleSeconds)) > Date() {
                        let next = ts.addingTimeInterval(TimeInterval(cycleSeconds))
                        Text("Next in ") + Text(next, style: .relative)
                    } else {
                        Text("\(decisions.count) decisions")
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
                Spacer()
                if !decisions.isEmpty {
                    Button { withAnimation(LakshmiTheme.springSnappy) { expanded.toggle() } } label: {
                        Image(systemName: expanded ? "chevron.up" : "chevron.down")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .padding()

            if decisions.isEmpty {
                Text("No AI decisions yet — waiting for first cycle.")
                    .font(.caption).foregroundStyle(.secondary).padding([.horizontal, .bottom])
            } else {
                Divider().padding(.horizontal)
                if let latest = decisions.first {
                    AIDecisionRow(event: latest, showFullMessage: true)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
                if expanded {
                    ForEach(decisions.dropFirst()) { event in
                        Divider().padding(.leading, 52)
                        AIDecisionRow(event: event, showFullMessage: false)
                            .transition(.move(edge: .top).combined(with: .opacity))
                    }
                }
                if decisions.count > 1 {
                    Button { withAnimation(LakshmiTheme.springSnappy) { expanded.toggle() } } label: {
                        HStack(spacing: 4) {
                            Text(expanded ? "Show less" : "Show \(decisions.count - 1) more")
                            Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.caption2)
                        }
                        .font(.caption).foregroundStyle(LakshmiTheme.blue)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 10)
                }
            }
        }
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
    }
}

// MARK: - Glass nav row (used in both BotTabView and MoreView)

struct GlassNavRow: View {
    let icon: String
    let title: String
    let subtitle: String
    let color: Color

    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(color.opacity(0.12))
                    .frame(width: 40, height: 40)
                Image(systemName: icon)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(color)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
        }
        .padding(14)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
    }
}

// MARK: - Bot settings page (drill-down from the quick row)

private struct BotSettingsPage: View {
    @StateObject private var vm = TradingViewModel()
    @StateObject private var insightsVM = InsightsViewModel()

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                BotControlView(vm: vm)
                StrategyPickerView()
                TradingFloorView()
                WatchlistEditorView()
                TradingBudgetCard()
                RiskSettingsCard()
                ModelPickerCard()
                NavigationLink {
                    PromptViewerView()
                } label: {
                    GlassNavRow(icon: "text.badge.plus", title: "AI Prompt",
                                subtitle: "View last prompt · set override instructions",
                                color: LakshmiTheme.purple)
                }
                .buttonStyle(PressScaleButtonStyle())
            }
            .padding()
        }
        .navigationTitle("Bot Configuration")
        .task { await vm.load() }
    }
}
