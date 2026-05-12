import SwiftUI

struct TradeHistoryView: View {
    @State private var trades: [PositionLog] = []
    @State private var summary: PerformanceSummary? = nil
    @State private var isLoading = true
    @State private var selectedTrade: PositionLog? = nil

    var body: some View {
        Group {
            if isLoading && trades.isEmpty {
                ProgressView("Loading trade history...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if trades.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "chart.bar.xaxis")
                        .font(.system(size: 40))
                        .foregroundStyle(.secondary)
                    Text("No closed trades yet")
                        .font(.headline)
                        .foregroundStyle(.secondary)
                    Text("Completed trades will appear here with full P&L context.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding()
            } else {
                ScrollView {
                    VStack(spacing: 16) {
                        // ── Performance summary card ──
                        if let s = summary {
                            PerformanceSummaryCard(summary: s)
                        }

                        // ── Trade list ──
                        VStack(spacing: 0) {
                            ForEach(trades) { trade in
                                TradeHistoryRow(trade: trade)
                                    .onTapGesture { selectedTrade = trade }
                                Divider().padding(.leading, 16)
                            }
                        }
                        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
                    }
                    .padding()
                }
                .refreshable { await load() }
            }
        }
        .navigationTitle("Trade History")
        .sheet(item: $selectedTrade) { trade in
            TradeDetailSheet(trade: trade)
        }
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        async let tradesTask = try? APIService.shared.getPositionHistory(limit: 100)
        async let summaryTask = try? APIService.shared.getPerformanceSummary()
        let (t, s) = await (tradesTask, summaryTask)
        trades = t ?? []
        summary = s
        isLoading = false
    }
}

// MARK: - Summary card

struct PerformanceSummaryCard: View {
    let summary: PerformanceSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Overall Performance")
                .font(.headline)

            HStack(spacing: 0) {
                StatPill(label: "Trades", value: "\(summary.total_trades)", color: .primary)
                StatPill(label: "Win Rate", value: String(format: "%.0f%%", summary.win_rate_pct),
                         color: summary.win_rate_pct >= 50 ? .green : .red)
                StatPill(label: "Total P&L",
                         value: String(format: "$%@%.0f", summary.total_realized_pl >= 0 ? "+" : "", summary.total_realized_pl),
                         color: summary.total_realized_pl >= 0 ? .green : .red)
            }

            HStack(spacing: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Avg Win").font(.caption).foregroundStyle(.secondary)
                    Text("+\(summary.avg_win_pct, specifier: "%.1f")%")
                        .font(.subheadline).fontWeight(.semibold).foregroundStyle(.green)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Avg Loss").font(.caption).foregroundStyle(.secondary)
                    Text("\(summary.avg_loss_pct, specifier: "%.1f")%")
                        .font(.subheadline).fontWeight(.semibold).foregroundStyle(.red)
                }
                Spacer()
            }

            if !summary.best_symbols.isEmpty {
                HStack(spacing: 6) {
                    Text("Best:").font(.caption).foregroundStyle(.secondary)
                    ForEach(summary.best_symbols.prefix(3)) { sym in
                        Text("\(sym.symbol)(+\(sym.avg_pct, specifier: "%.1f")%)")
                            .font(.caption2).fontWeight(.medium)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Color.green.opacity(0.15))
                            .foregroundStyle(.green)
                            .clipShape(Capsule())
                    }
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }
}

struct StatPill: View {
    let label: String; let value: String; let color: Color
    var body: some View {
        VStack(spacing: 2) {
            Text(value).font(.subheadline).fontWeight(.bold).foregroundStyle(color)
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Trade row

struct TradeHistoryRow: View {
    let trade: PositionLog

    var body: some View {
        HStack(spacing: 12) {
            // Win/loss indicator
            RoundedRectangle(cornerRadius: 3)
                .fill(trade.isWin ? Color.green : Color.red)
                .frame(width: 4)
                .padding(.vertical, 8)

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(trade.symbol)
                        .font(.subheadline).fontWeight(.semibold)
                    if trade.isShort {
                        Text("SHORT")
                            .font(.caption2).fontWeight(.bold)
                            .foregroundStyle(.white)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Color.orange)
                            .clipShape(Capsule())
                    }
                    Text(trade.exitReasonLabel)
                        .font(.caption2).fontWeight(.medium)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Color(.systemGray5))
                        .clipShape(Capsule())
                    Spacer()
                    if let pct = trade.realized_pl_pct {
                        Text("\(pct >= 0 ? "+" : "")\(pct, specifier: "%.1f")%")
                            .font(.subheadline).fontWeight(.bold)
                            .foregroundStyle(pct >= 0 ? .green : .red)
                    }
                }
                HStack(spacing: 8) {
                    if let ep = trade.entry_price, let xp = trade.exit_price {
                        Text("$\(ep, specifier: "%.2f") → $\(xp, specifier: "%.2f")")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if let qty = trade.quantity {
                        Text("×\(qty)").font(.caption).foregroundStyle(.tertiary)
                    }
                    Spacer()
                    Text(trade.holdDurationText)
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }
}

// MARK: - Detail sheet

struct TradeDetailSheet: View {
    let trade: PositionLog
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    // Header
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(trade.symbol).font(.largeTitle).fontWeight(.bold)
                            if trade.exit_reason != nil {
                                Text(trade.exitReasonLabel)
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                        if let pct = trade.realized_pl_pct {
                            VStack(alignment: .trailing) {
                                Text("\(pct >= 0 ? "+" : "")\(pct, specifier: "%.2f")%")
                                    .font(.title2).fontWeight(.bold)
                                    .foregroundStyle(pct >= 0 ? .green : .red)
                                if let pl = trade.realized_pl {
                                    Text("\(pl >= 0 ? "+" : "")$\(pl, specifier: "%.2f")")
                                        .font(.subheadline).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    .padding()
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                    // Trade details
                    VStack(spacing: 0) {
                        DetailRow(label: "Entry Price", value: trade.entry_price.map { String(format: "$%.2f", $0) } ?? "—")
                        Divider().padding(.leading, 16)
                        DetailRow(label: "Exit Price", value: trade.exit_price.map { String(format: "$%.2f", $0) } ?? "—")
                        Divider().padding(.leading, 16)
                        DetailRow(label: "Quantity", value: trade.quantity.map { "\($0) shares" } ?? "—")
                        Divider().padding(.leading, 16)
                        DetailRow(label: "Hold Duration", value: trade.holdDurationText)
                        Divider().padding(.leading, 16)
                        DetailRow(label: "Strategy", value: trade.strategy?.capitalized ?? "—")
                        Divider().padding(.leading, 16)
                        DetailRow(label: "Market Regime", value: trade.market_regime?.uppercased() ?? "—")
                    }
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                    // Claude's reasoning
                    if let reasoning = trade.claude_reasoning, !reasoning.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Claude's Reasoning", systemImage: "brain")
                                .font(.headline)
                            Text(reasoning)
                                .font(.subheadline)
                                .foregroundStyle(.primary)
                                .lineSpacing(4)
                        }
                        .padding()
                        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
                    }
                }
                .padding()
            }
            .navigationTitle("Trade Detail")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}

struct DetailRow: View {
    let label: String; let value: String
    var body: some View {
        HStack {
            Text(label).font(.subheadline).foregroundStyle(.secondary)
            Spacer()
            Text(value).font(.subheadline).fontWeight(.medium)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}
