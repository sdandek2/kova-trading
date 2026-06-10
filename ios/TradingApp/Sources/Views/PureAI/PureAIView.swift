import SwiftUI

// ── Pure-AI Experiment Tab ─────────────────────────────────────────────────────
// Ablation test: this account has NO signal pipeline.
// Claude Opus 4.8 searches the web each cycle, decides alone.
// Compare vs Kova after 30 days.

struct PureAIView: View {
    @StateObject private var vm = PureAIViewModel()
    @State private var selectedTab = 0

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    if let err = vm.status?.accountError {
                        AccountErrorBanner(message: err)
                    }

                    PureAIHeroCard(vm: vm)

                    Picker("Tab", selection: $selectedTab) {
                        Text("Holdings").tag(0)
                        Text("Decisions").tag(1)
                        Text("Settings").tag(2)
                    }
                    .pickerStyle(.segmented)

                    switch selectedTab {
                    case 0: PureAIHoldingsSection(vm: vm)
                    case 1: PureAIDecisionsSection(vm: vm)
                    default: PureAISettingsSection(vm: vm)
                    }

                    Spacer(minLength: 32)
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
            }
            .background(KovaTheme.pageBackground.ignoresSafeArea())
            .navigationTitle("Pure-AI")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    HStack(spacing: 6) {
                        KovaChip(text: vm.isPaper ? "PAPER" : "LIVE",
                                 color: vm.isPaper ? .orange : KovaTheme.positive)
                        KovaChip(text: vm.modelDisplay,
                                 color: KovaTheme.purple)
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task {
                            await vm.loadStatus()
                            if selectedTab == 1 { await vm.loadDecisions() }
                        }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 15))
                    }
                    .disabled(vm.isLoading)
                }
            }
            .refreshable {
                await vm.loadStatus()
                if selectedTab == 1 { await vm.loadDecisions() }
            }
            .task {
                await vm.loadStatus()
                await vm.loadDecisions()
            }
            .onChange(of: selectedTab) { tab in
                if tab == 1 { Task { await vm.loadDecisions() } }
            }
            .alert("Error", isPresented: Binding(
                get: { vm.errorMessage != nil },
                set: { if !$0 { vm.errorMessage = nil } }
            )) {
                Button("OK", role: .cancel) { vm.errorMessage = nil }
            } message: {
                Text(vm.errorMessage ?? "")
            }
            .sheet(isPresented: $vm.showCycleSheet) {
                PureAICycleResultSheet(result: vm.cycleResult)
            }
            .overlay {
                if vm.isLoading && vm.status == nil {
                    ProgressView("Loading…")
                        .padding(24)
                        .background(.regularMaterial)
                        .clipShape(RoundedRectangle(cornerRadius: 16))
                }
            }
        }
    }
}

// MARK: - Account error banner

private struct AccountErrorBanner: View {
    let message: String
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.white)
            Text(message).font(.caption).foregroundStyle(.white).lineLimit(2)
        }
        .padding(12)
        .background(KovaTheme.negative)
        .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
    }
}

// MARK: - Hero Card

private struct PureAIHeroCard: View {
    @ObservedObject var vm: PureAIViewModel

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                LinearGradient(colors: [KovaTheme.purple, KovaTheme.blue],
                               startPoint: .topLeading, endPoint: .bottomTrailing)
                VStack(spacing: 6) {
                    Text("Portfolio Value")
                        .font(.caption).foregroundStyle(.white.opacity(0.75))
                    Text(vm.equityDisplay)
                        .font(.system(size: 36, weight: .bold, design: .rounded))
                        .foregroundStyle(.white)
                    if let pl = vm.status?.realizedPl, pl != 0 {
                        Text(vm.realizedPlDisplay + " realized")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(vm.realizedPlPositive ? KovaTheme.positive : KovaTheme.negative)
                    }
                }
                .padding(.vertical, 22)
            }
            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radius))

            // Stats row
            HStack(spacing: 0) {
                StatCell(label: "Positions",
                         value: "\(vm.holdingsCount)")
                Divider().frame(height: 36)
                StatCell(label: "Closed Trades",
                         value: "\(vm.status?.closedTrades ?? 0)")
                Divider().frame(height: 36)
                StatCell(label: "Win Rate",
                         value: vm.status?.winRate.map { String(format: "%.0f%%", $0) } ?? "--")
            }
            .padding(.vertical, 8)
            .background(KovaTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radius))
            .padding(.top, 4)
        }
    }
}

private struct StatCell: View {
    let label: String
    let value: String
    var body: some View {
        VStack(spacing: 2) {
            Text(value).font(.headline.weight(.bold))
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Holdings Section

private struct PureAIHoldingsSection: View {
    @ObservedObject var vm: PureAIViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader(title: "Current Holdings",
                          subtitle: "\(vm.holdingsCount) positions")
            if let holdings = vm.status?.holdings, !holdings.isEmpty {
                ForEach(holdings) { h in
                    PureAIHoldingRow(holding: h)
                }
            } else if vm.status != nil {
                Text("No open positions — all cash")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 20)
                    .kovaCard()
            }

            // Run cycle button
            Button {
                Task { await vm.runCycle() }
            } label: {
                HStack(spacing: 8) {
                    if vm.isRunning {
                        ProgressView().tint(.white).scaleEffect(0.85)
                        Text("AI is thinking & searching…").fontWeight(.semibold)
                    } else {
                        Image(systemName: "brain.head.profile").font(.system(size: 15))
                        Text("Run AI Cycle Now").fontWeight(.semibold)
                    }
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background {
                    if vm.isRunning {
                        KovaTheme.purple.opacity(0.5)
                    } else {
                        LinearGradient(colors: [KovaTheme.purple, KovaTheme.blue],
                                       startPoint: .leading, endPoint: .trailing)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
            }
            .disabled(vm.isRunning)
        }
    }
}

private struct PureAIHoldingRow: View {
    let holding: PureAIHolding
    var body: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(holding.isUp ? KovaTheme.positive.opacity(0.15) : KovaTheme.negative.opacity(0.15))
                .frame(width: 42, height: 42)
                .overlay(
                    Text(String(holding.symbol.prefix(2)))
                        .font(.caption.weight(.bold))
                        .foregroundStyle(holding.isUp ? KovaTheme.positive : KovaTheme.negative)
                )
            VStack(alignment: .leading, spacing: 2) {
                Text(holding.symbol).font(.headline)
                Text(String(format: "%.4f shares @ $%.2f", holding.qty, holding.entryPrice))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(String(format: "$%.2f", holding.currentPrice))
                    .font(.subheadline.weight(.semibold))
                Text(String(format: "%@$%.2f (%.1f%%)",
                            holding.isUp ? "+" : "",
                            holding.unrealizedPl,
                            holding.unrealizedPlPct))
                    .font(.caption.weight(.medium))
                    .foregroundStyle(holding.isUp ? KovaTheme.positive : KovaTheme.negative)
            }
        }
        .padding(14)
        .kovaCard()
    }
}

// MARK: - Decisions Section

private struct PureAIDecisionsSection: View {
    @ObservedObject var vm: PureAIViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionHeader(title: "Decision Log",
                          subtitle: "Last \(vm.decisions.count) cycles")
            if vm.decisions.isEmpty {
                Text("No decisions yet — run a cycle to start")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.vertical, 20)
                    .kovaCard()
            } else {
                ForEach(vm.decisions) { d in
                    PureAIDecisionCard(decision: d, vm: vm)
                }
            }
        }
    }
}

private struct PureAIDecisionCard: View {
    let decision: PureAIDecision
    @ObservedObject var vm: PureAIViewModel
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Header row
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(vm.formattedTimestamp(decision.timestamp))
                        .font(.caption.weight(.semibold))
                    if let mv = decision.decision?.marketView {
                        Text(mv).font(.caption).foregroundStyle(.secondary).lineLimit(expanded ? nil : 2)
                    }
                }
                Spacer()
                if let err = decision.error {
                    KovaChip(text: "Error", color: KovaTheme.negative)
                        .help(err)
                } else {
                    let count = (decision.executed?.filter { $0.status == "submitted" }.count) ?? 0
                    KovaChip(text: count > 0 ? "\(count) trades" : "No trades",
                             color: count > 0 ? KovaTheme.positive : .gray)
                }
            }

            // Searches
            if let searches = decision.searches, !searches.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Searched (\(searches.count)):")
                        .font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                    ForEach(searches, id: \.self) { q in
                        HStack(spacing: 4) {
                            Image(systemName: "magnifyingglass").font(.caption2).foregroundStyle(.secondary)
                            Text(q).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                }
            }

            // Executed orders (expanded only)
            if expanded, let orders = decision.executed, !orders.isEmpty {
                Divider()
                ForEach(orders) { o in
                    HStack {
                        KovaChip(text: o.action.uppercased(),
                                 color: o.action == "sell" ? KovaTheme.negative : KovaTheme.positive)
                        Text(o.symbol).font(.caption.weight(.bold))
                        if let dollars = o.dollars {
                            Text(String(format: "$%.0f", dollars)).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        KovaChip(text: o.status,
                                 color: o.status == "submitted" ? KovaTheme.positive :
                                        o.status == "rejected" ? .orange : KovaTheme.negative)
                    }
                    if let note = o.thesis ?? o.reason ?? o.why {
                        Text(note).font(.caption2).foregroundStyle(.secondary).padding(.leading, 4)
                    }
                }
            }

            Button {
                withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() }
            } label: {
                HStack(spacing: 4) {
                    Text(expanded ? "Show less" : "Show more")
                        .font(.caption2.weight(.semibold))
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                }
                .foregroundStyle(KovaTheme.purple)
            }
        }
        .padding(14)
        .kovaCard()
    }
}

// MARK: - Settings Section

private struct PureAISettingsSection: View {
    @ObservedObject var vm: PureAIViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Experiment Settings", subtitle: "Stored server-side")

            VStack(spacing: 0) {
                Toggle("Enabled", isOn: $vm.editEnabled)
                    .padding(16)
                Divider()
                // Model picker
                HStack {
                    Text("AI Model").font(.subheadline)
                    Spacer()
                    Picker("Model", selection: $vm.editModel) {
                        ForEach(vm.modelOptions, id: \.self) { m in
                            Text(m.replacingOccurrences(of: "claude-", with: "")
                                  .replacingOccurrences(of: "-", with: " ")
                                  .capitalized)
                                .tag(m)
                        }
                    }
                    .pickerStyle(.menu)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                Divider()
                StepperRow(label: "Max buys per cycle",
                           value: $vm.editMaxBuys, range: 1...10)
                Divider()
                StepperRow(label: "Cycle interval (minutes)",
                           value: $vm.editInterval, range: 10...240, step: 10)
                Divider()
                StepperRow(label: "Max web searches",
                           value: $vm.editMaxSearches, range: 2...15)
            }
            .background(KovaTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radius))

            Button {
                Task { await vm.saveConfig() }
            } label: {
                HStack(spacing: 6) {
                    if vm.isSavingConfig {
                        ProgressView().tint(.white).scaleEffect(0.85)
                    } else {
                        Image(systemName: "checkmark.circle.fill")
                    }
                    Text("Save Settings").fontWeight(.semibold)
                }
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(
                    vm.isSavingConfig
                    ? KovaTheme.purple.opacity(0.5)
                    : KovaTheme.purple
                )
                .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
            }
            .disabled(vm.isSavingConfig)

            VStack(alignment: .leading, spacing: 6) {
                Text("About this experiment")
                    .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Text("The selected AI model receives only its own portfolio + today's date. It searches the web, picks stocks, and executes — no signal pipeline, no curated data. After 30 days we compare its Sharpe, win rate, and drawdown against Kova. Use Opus 4.8 for best results; Sonnet 4.6 or Haiku 4.5 to reduce cost.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(14)
            .background(KovaTheme.purple.opacity(0.07))
            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
        }
    }
}

// MARK: - Cycle Result Sheet

struct PureAICycleResultSheet: View {
    let result: PureAICycleResult?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let mv = result?.marketView {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Market View").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            Text(mv).font(.subheadline)
                        }
                        .padding(14).background(KovaTheme.card).clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
                    }

                    if let searches = result?.searches, !searches.isEmpty {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Searches (\(searches.count))").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            ForEach(searches, id: \.self) { q in
                                HStack(spacing: 6) {
                                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary).font(.caption)
                                    Text(q).font(.caption)
                                }
                            }
                        }
                        .padding(14).background(KovaTheme.card).clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
                    }

                    if let orders = result?.executed, !orders.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Executed (\(orders.count))").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                            ForEach(orders) { o in
                                HStack {
                                    KovaChip(text: o.action.uppercased(),
                                             color: o.action == "sell" ? KovaTheme.negative : KovaTheme.positive)
                                    Text(o.symbol).font(.subheadline.weight(.bold))
                                    if let d = o.dollars { Text(String(format: "$%.0f", d)).font(.caption).foregroundStyle(.secondary) }
                                    Spacer()
                                    KovaChip(text: o.status,
                                             color: o.status == "submitted" ? KovaTheme.positive :
                                                    o.status == "rejected" ? .orange : KovaTheme.negative)
                                }
                                if let note = o.thesis ?? o.reason ?? o.why {
                                    Text(note).font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                        .padding(14).background(KovaTheme.card).clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
                    }

                    if let err = result?.error {
                        Text("Error: \(err)").font(.caption).foregroundStyle(KovaTheme.negative)
                            .padding(14).background(KovaTheme.negative.opacity(0.1))
                            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radiusSm))
                    }

                    if result?.marketView == nil && (result?.executed?.isEmpty ?? true) && result?.error == nil {
                        Text("Cycle complete — AI chose to hold (no trades this cycle).")
                            .font(.subheadline).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .center)
                            .padding(.vertical, 20)
                    }
                }
                .padding(16)
            }
            .background(KovaTheme.pageBackground.ignoresSafeArea())
            .navigationTitle("Cycle Result")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}

// MARK: - Shared helpers

private struct SectionHeader: View {
    let title: String
    let subtitle: String
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.headline)
            Spacer()
            Text(subtitle).font(.caption).foregroundStyle(.secondary)
        }
    }
}

private struct StepperRow: View {
    let label: String
    @Binding var value: Int
    let range: ClosedRange<Int>
    var step: Int = 1

    var body: some View {
        HStack {
            Text(label).font(.subheadline)
            Spacer()
            Stepper("\(value)", value: $value, in: range, step: step)
                .labelsHidden()
            Text("\(value)")
                .font(.subheadline.weight(.semibold))
                .frame(minWidth: 36, alignment: .trailing)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}
