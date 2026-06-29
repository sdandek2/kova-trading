import SwiftUI

struct LabsView: View {
    @StateObject private var vm = LabsViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    enginePicker
                    if vm.selectedEngine == .secIntel {
                        secIntelStatusCard
                        secIntelPerformanceCard
                        secIntelOpenPositions
                        secIntelSignalsSection
                        secIntelHistorySection
                    } else {
                        statusCard
                        openPositionsSection
                        closedPositionsSection
                    }
                }
                .padding(.horizontal, LakshmiTheme.pagePad)
                .padding(.top, 8)
                .padding(.bottom, 100)
            }
            .background(LakshmiTheme.pageBackground)
            .navigationTitle("Labs")
            .navigationBarTitleDisplayMode(.large)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await vm.triggerScan() }
                    } label: {
                        if vm.isRunning {
                            ProgressView().scaleEffect(0.8)
                        } else {
                            Image(systemName: "bolt.fill")
                                .foregroundStyle(LakshmiTheme.gold)
                        }
                    }
                    .disabled(vm.isRunning)
                }
                ToolbarItem(placement: .topBarLeading) {
                    if let refreshed = vm.lastRefreshed {
                        Text(refreshed, style: .time)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
        .task { await vm.loadAll() }
        .onAppear { vm.startAutoRefresh() }
        .onDisappear { vm.stopAutoRefresh() }
        .alert("Error", isPresented: .constant(vm.errorMessage != nil)) {
            Button("Dismiss") { vm.errorMessage = nil }
        } message: {
            Text(vm.errorMessage ?? "")
        }
    }

    // ── SEC Intel: Status card ────────────────────────────────────────────────

    private var secIntelStatusCard: some View {
        VStack(spacing: 0) {
            if let st = vm.secIntelStatus {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            if st.threadAlive {
                                LiveDot()
                            } else {
                                Circle()
                                    .fill(st.configured ? Color.orange : Color.gray)
                                    .frame(width: 8, height: 8)
                            }
                            Text(st.threadAlive ? "Running" : (st.configured ? "Stopped" : "Not configured"))
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.secondary)
                        }
                        Text("SEC Intelligence")
                            .font(.system(size: 24, weight: .bold, design: .rounded))
                        Text("Institutional 13F following")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 8) {
                        statPill(label: "Open", value: "\(st.openPositions)/\(st.maxPositions)")
                        statPill(label: "Signals", value: "\(st.signalCount180d)",
                                 color: LakshmiTheme.blue)
                        statPill(label: "Mode", value: "Paper",
                                 color: .orange)
                    }
                }
                .padding(LakshmiTheme.cardPad)
            } else if vm.isLoading {
                HStack { Spacer(); ProgressView(); Spacer() }
                    .padding(LakshmiTheme.cardPad)
            } else {
                Text("SEC Intel not configured — set ALPACA_SEC_INTEL_KEY in Railway")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(LakshmiTheme.cardPad)
            }
        }
        .kovaAccentCard(padded: false)
        .cardAppear(delay: 0.05)
    }

    // ── SEC Intel: Performance card ───────────────────────────────────────────

    @ViewBuilder
    private var secIntelPerformanceCard: some View {
        if let perf = vm.secIntelPerformance, let total = perf.totalTrades, total > 0 {
            VStack(spacing: 12) {
                HStack {
                    Text("Performance")
                        .font(.headline)
                    Spacer()
                    Text("\(total) trades")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                HStack(spacing: 12) {
                    perfStat(label: "Win Rate", value: String(format: "%.0f%%", perf.winRate ?? 0),
                             color: (perf.winRate ?? 0) >= 55 ? LakshmiTheme.positive : LakshmiTheme.negative)
                    perfStat(label: "Net P&L",
                             value: String(format: "%+.0f", perf.netPl ?? 0),
                             color: (perf.netPl ?? 0) >= 0 ? LakshmiTheme.positive : LakshmiTheme.negative)
                    perfStat(label: "Avg Hold",
                             value: String(format: "%.0fd", perf.avgHoldDays ?? 0),
                             color: LakshmiTheme.gold)
                }
            }
            .padding(LakshmiTheme.cardPad)
            .kovaCard(padded: false)
            .cardAppear(delay: 0.08)
        }
    }

    private func perfStat(label: String, value: String, color: Color) -> some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.system(size: 18, weight: .bold, design: .rounded))
                .foregroundStyle(color)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 10)
        .background(color.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    // ── SEC Intel: Open positions ─────────────────────────────────────────────

    @ViewBuilder
    private var secIntelOpenPositions: some View {
        if !vm.secIntelPositions.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                sectionHeader("Open Positions", count: vm.secIntelPositions.count)
                ForEach(vm.secIntelPositions) { pos in
                    SecIntelPositionRow(pos: pos)
                }
            }
            .cardAppear(delay: 0.10)
        }
    }

    // ── SEC Intel: Signals ────────────────────────────────────────────────────

    @ViewBuilder
    private var secIntelSignalsSection: some View {
        if !vm.secIntelSignals.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                sectionHeader("Active Signals (180d)", count: vm.secIntelSignals.count)
                ForEach(vm.secIntelSignals.prefix(20)) { sig in
                    SecIntelSignalRow(signal: sig)
                }
            }
            .cardAppear(delay: 0.12)
        }
    }

    // ── SEC Intel: History ────────────────────────────────────────────────────

    @ViewBuilder
    private var secIntelHistorySection: some View {
        if !vm.secIntelHistory.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                sectionHeader("Closed Trades", count: vm.secIntelHistory.count)
                ForEach(vm.secIntelHistory) { trade in
                    SecIntelTradeRow(trade: trade)
                }
            }
            .cardAppear(delay: 0.15)
        } else if vm.selectedEngine == .secIntel && !vm.isLoading {
            VStack(spacing: 8) {
                Image(systemName: "building.columns")
                    .font(.system(size: 36))
                    .foregroundStyle(.tertiary)
                Text("No trades yet")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Text("Signals process weekly. Add Alpaca + Telegram keys in Railway to activate.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(32)
            .cardAppear(delay: 0.15)
        }
    }

    // ── Engine picker ─────────────────────────────────────────────────────────

    private var enginePicker: some View {
        HStack(spacing: 0) {
            ForEach(LabsViewModel.Engine.allCases, id: \.self) { engine in
                let sel = engine == vm.selectedEngine
                Button {
                    Task { await vm.switchEngine(engine) }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: engine.icon)
                            .font(.caption.weight(.semibold))
                        Text(engine.displayName)
                            .font(.caption.weight(.semibold))
                    }
                    .foregroundStyle(sel ? .white : .secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity)
                    .background {
                        if sel {
                            RoundedRectangle(cornerRadius: 10)
                                .fill(LakshmiTheme.brandGradient)
                                .shadow(color: LakshmiTheme.gold.opacity(0.3), radius: 6)
                        }
                    }
                }
                .buttonStyle(PressScaleButtonStyle(scale: 0.94))
            }
        }
        .padding(4)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .animation(LakshmiTheme.springSnappy, value: vm.selectedEngine)
    }

    // ── Status card ───────────────────────────────────────────────────────────

    private var statusCard: some View {
        VStack(spacing: 0) {
            if let st = vm.currentStatus {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            if st.running {
                                LiveDot()
                            } else {
                                Circle()
                                    .fill(st.configured ? Color.orange : Color.gray)
                                    .frame(width: 8, height: 8)
                            }
                            Text(st.running ? "Running" : (st.configured ? "Configured" : "Not configured"))
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.secondary)
                        }
                        if let equity = st.equity {
                            Text(equity, format: .currency(code: "USD"))
                                .font(.system(size: 28, weight: .bold, design: .rounded))
                                .foregroundStyle(.primary)
                        } else {
                            Text("—")
                                .font(.system(size: 28, weight: .bold, design: .rounded))
                                .foregroundStyle(.secondary)
                        }
                        Text("Account Equity")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 8) {
                        statPill(label: "Open", value: "\(st.openPositions ?? 0)")
                        statPill(label: "Closed", value: "\(st.closedTrades ?? 0)")
                        if let wr = st.winRate {
                            statPill(label: "Win Rate", value: String(format: "%.0f%%", wr),
                                     color: wr >= 55 ? LakshmiTheme.positive : LakshmiTheme.negative)
                        }
                    }
                }
                .padding(LakshmiTheme.cardPad)

                if let pl = st.realizedPl, pl != 0 {
                    Divider().padding(.horizontal, LakshmiTheme.cardPad)
                    HStack {
                        Text("Realized P&L")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(pl >= 0 ? "+\(String(format: "%.2f", pl))" : String(format: "%.2f", pl))
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(pl >= 0 ? LakshmiTheme.positive : LakshmiTheme.negative)
                    }
                    .padding(.horizontal, LakshmiTheme.cardPad)
                    .padding(.vertical, 10)
                }
            } else if vm.isLoading {
                HStack { Spacer(); ProgressView(); Spacer() }
                    .padding(LakshmiTheme.cardPad)
            } else {
                Text("Status unavailable")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(LakshmiTheme.cardPad)
            }
        }
        .kovaAccentCard(padded: false)
        .cardAppear(delay: 0.05)
    }

    private func statPill(label: String, value: String,
                          color: Color = LakshmiTheme.gold) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.system(size: 15, weight: .bold, design: .rounded))
                .foregroundStyle(color)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(color.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    // ── Open positions ────────────────────────────────────────────────────────

    @ViewBuilder
    private var openPositionsSection: some View {
        if !vm.openPositions.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                sectionHeader("Open Positions", count: vm.openPositions.count)
                ForEach(vm.openPositions) { pos in
                    PositionRow(pos: pos, engine: vm.selectedEngine) {
                        Task { await vm.closePosition(pos.id) }
                    }
                }
            }
            .cardAppear(delay: 0.10)
        }
    }

    // ── Closed positions ──────────────────────────────────────────────────────

    @ViewBuilder
    private var closedPositionsSection: some View {
        if !vm.closedPositions.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                sectionHeader("Recent Closed", count: vm.closedPositions.count)
                ForEach(vm.closedPositions.prefix(10)) { pos in
                    ClosedPositionRow(pos: pos)
                }
            }
            .cardAppear(delay: 0.15)
        } else if !vm.isLoading && vm.positions.isEmpty {
            VStack(spacing: 8) {
                Image(systemName: "flask")
                    .font(.system(size: 36))
                    .foregroundStyle(.tertiary)
                Text("No trades yet")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Text("First scan runs at 9:50 AM ET on market days.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity)
            .padding(32)
            .cardAppear(delay: 0.15)
        }
    }

    private func sectionHeader(_ title: String, count: Int) -> some View {
        HStack {
            Text(title)
                .font(.headline)
            Spacer()
            Text("\(count)")
                .font(.caption.weight(.bold))
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(LakshmiTheme.amber)
                .clipShape(Capsule())
        }
    }
}

// ── Position row (open) ───────────────────────────────────────────────────────

private struct PositionRow: View {
    let pos: ExperimentPosition
    let engine: LabsViewModel.Engine
    let onClose: () -> Void
    @State private var showConfirm = false

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(pos.symbol)
                        .font(.headline)
                    engineBadge
                }
                if let entry = pos.entryPrice {
                    Text("Entry $\(String(format: "%.2f", entry))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                metaLabel
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 6) {
                if let stop = pos.stopPrice {
                    Text("Stop $\(String(format: "%.2f", stop))")
                        .font(.caption2)
                        .foregroundStyle(LakshmiTheme.negative)
                }
                if let target = pos.targetPrice {
                    Text("Target $\(String(format: "%.2f", target))")
                        .font(.caption2)
                        .foregroundStyle(LakshmiTheme.positive)
                }
                Button("Close") { showConfirm = true }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(LakshmiTheme.negative)
            }
        }
        .padding(LakshmiTheme.cardPad)
        .kovaCard(padded: false)
        .confirmationDialog("Close \(pos.symbol)?", isPresented: $showConfirm) {
            Button("Close at Market", role: .destructive, action: onClose)
            Button("Cancel", role: .cancel) {}
        }
    }

    @ViewBuilder
    private var engineBadge: some View {
        switch engine {
        case .squeeze:
            if let dtc = pos.daysToCover {
                LakshmiChip(text: "DTC \(String(format: "%.1f", dtc))", color: .orange)
            }
        case .spillover:
            if let trig = pos.triggerSymbol {
                LakshmiChip(text: "via \(trig)", color: LakshmiTheme.blue)
            }
        case .revision:
            if let bp = pos.beatPct {
                LakshmiChip(text: "+\(String(format: "%.0f", bp))% beat", color: LakshmiTheme.positive)
            }
        case .secIntel:
            EmptyView()
        }
    }

    @ViewBuilder
    private var metaLabel: some View {
        switch engine {
        case .squeeze:
            if let vr = pos.volumeRatio {
                Text(String(format: "Volume ×%.1f", vr))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        case .spillover:
            if let bp = pos.triggerBeatPct {
                Text(String(format: "Beat +%.0f%%", bp))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        case .revision, .secIntel:
            if let date = pos.entryDate {
                Text("Since \(date.prefix(10))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

// ── SEC Intel rows ────────────────────────────────────────────────────────────

private struct SecIntelPositionRow: View {
    let pos: SecIntelPosition

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(pos.ticker)
                    .font(.headline)
                if let inst = pos.institution {
                    LakshmiChip(text: inst, color: LakshmiTheme.gold)
                }
                Spacer()
                if let entry = pos.entryPrice {
                    Text("Entry $\(String(format: "%.2f", entry))")
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                }
            }
            HStack(spacing: 12) {
                if let stop = pos.stop {
                    label("Stop", "$\(String(format: "%.2f", stop))", LakshmiTheme.negative)
                }
                if pos.l1Exit != nil {
                    label("L1 Done", "✓", LakshmiTheme.positive)
                }
                if pos.l2Exit != nil {
                    label("L2 Done", "✓", LakshmiTheme.positive)
                }
                if let trail = pos.trailStop {
                    label("Trail", "$\(String(format: "%.2f", trail))", LakshmiTheme.amber)
                }
                Spacer()
                if let maxHold = pos.maxHold {
                    Text("Until \(maxHold.prefix(10))")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }
        }
        .padding(LakshmiTheme.cardPad)
        .kovaCard(padded: false)
    }

    private func label(_ title: String, _ value: String, _ color: Color) -> some View {
        VStack(spacing: 1) {
            Text(value).font(.caption.weight(.semibold)).foregroundStyle(color)
            Text(title).font(.caption2).foregroundStyle(.secondary)
        }
    }
}

private struct SecIntelSignalRow: View {
    let signal: SecIntelSignal

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(signal.ticker)
                        .font(.subheadline.weight(.semibold))
                    Text(signal.action)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(signal.action == "NEW" ? LakshmiTheme.positive : LakshmiTheme.amber)
                        .clipShape(Capsule())
                }
                Text(signal.institution)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 3) {
                if signal.isWhitelist {
                    Text("⭐ WHITELIST")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(LakshmiTheme.gold)
                } else {
                    Text("Score \(signal.score)")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(signal.score >= 6 ? LakshmiTheme.positive : .secondary)
                }
                Text(signal.quarter)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.horizontal, LakshmiTheme.cardPad)
        .padding(.vertical, 10)
        .kovaCard(padded: false)
    }
}

private struct SecIntelTradeRow: View {
    let trade: SecIntelTrade

    private var pl: Double { trade.pl ?? 0 }
    private var isWin: Bool { pl > 0 }

    var body: some View {
        HStack {
            HStack(spacing: 8) {
                Image(systemName: isWin ? "checkmark.circle.fill" : "xmark.circle.fill")
                    .foregroundStyle(isWin ? LakshmiTheme.positive : LakshmiTheme.negative)
                VStack(alignment: .leading, spacing: 2) {
                    Text(trade.ticker)
                        .font(.subheadline.weight(.semibold))
                    if let reason = trade.reason {
                        Text(reason.replacingOccurrences(of: "_", with: " ").capitalized)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if let inst = trade.institution {
                        Text(inst)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(pl >= 0 ? "+$\(String(format: "%.2f", pl))" : "-$\(String(format: "%.2f", abs(pl)))")
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(isWin ? LakshmiTheme.positive : LakshmiTheme.negative)
                if let pct = trade.plPct {
                    Text(String(format: "%+.1f%%", pct))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, LakshmiTheme.cardPad)
        .padding(.vertical, 12)
        .kovaCard(padded: false)
    }
}

// ── Closed position row ───────────────────────────────────────────────────────

private struct ClosedPositionRow: View {
    let pos: ExperimentPosition

    private var pl: Double { pos.realizedPl ?? 0 }
    private var isWin: Bool { pl > 0 }

    var body: some View {
        HStack {
            HStack(spacing: 8) {
                Image(systemName: isWin ? "checkmark.circle.fill" : "xmark.circle.fill")
                    .foregroundStyle(isWin ? LakshmiTheme.positive : LakshmiTheme.negative)
                VStack(alignment: .leading, spacing: 2) {
                    Text(pos.symbol)
                        .font(.subheadline.weight(.semibold))
                    if let reason = pos.notes?.components(separatedBy: "exit: ").last {
                        Text(reason.replacingOccurrences(of: "_", with: " ").capitalized)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            Spacer()
            Text(pl >= 0 ? "+$\(String(format: "%.2f", pl))" : "-$\(String(format: "%.2f", abs(pl)))")
                .font(.subheadline.weight(.bold))
                .foregroundStyle(isWin ? LakshmiTheme.positive : LakshmiTheme.negative)
        }
        .padding(.horizontal, LakshmiTheme.cardPad)
        .padding(.vertical, 12)
        .kovaCard(padded: false)
    }
}
