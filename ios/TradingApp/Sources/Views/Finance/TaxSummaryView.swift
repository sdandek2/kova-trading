import SwiftUI

struct TaxSummaryView: View {
    @State private var summary: TaxSummary? = nil
    @State private var selectedRate: Double = 0.22
    @State private var isLoading = true
    @State private var error: String? = nil

    private let brackets: [(label: String, rate: Double)] = [
        ("10%", 0.10), ("12%", 0.12), ("22%", 0.22),
        ("24%", 0.24), ("32%", 0.32), ("35%", 0.35), ("37%", 0.37)
    ]

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // ── Bracket picker ──
                VStack(alignment: .leading, spacing: 8) {
                    Text("Your Tax Bracket")
                        .font(.headline)
                    Text("All bot gains are short-term (held days, not years). Select your income bracket:")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(brackets, id: \.rate) { b in
                                Button(b.label) {
                                    selectedRate = b.rate
                                    Task { await load() }
                                }
                                .font(.subheadline).fontWeight(.semibold)
                                .padding(.horizontal, 14).padding(.vertical, 8)
                                .background(selectedRate == b.rate ? Color.blue : Color(.systemGray5))
                                .foregroundStyle(selectedRate == b.rate ? .white : .primary)
                                .clipShape(Capsule())
                            }
                        }
                        .padding(.vertical, 2)
                    }
                }
                .padding()
                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                if isLoading {
                    ProgressView("Loading tax data…").padding(.top, 40)
                } else if let err = error {
                    Text(err).foregroundStyle(.red).padding()
                } else if let s = summary {
                    // ── YTD P&L card ──
                    VStack(alignment: .leading, spacing: 0) {
                        SectionHeader(title: "YTD REALIZED P&L", icon: "chart.bar.fill", color: .blue)
                        Divider().padding(.horizontal)

                        FinanceRow(label: "Total Gains",
                                   value: formatDollar(s.ytd.totalGains),
                                   valueColor: .green)
                        Divider().padding(.leading, 16)
                        FinanceRow(label: "Total Losses",
                                   value: formatDollar(s.ytd.totalLosses),
                                   valueColor: s.ytd.totalLosses < 0 ? .red : .primary)
                        Divider().padding(.leading, 16)
                        FinanceRow(label: "Net P&L",
                                   value: formatDollar(s.ytd.netPl),
                                   valueColor: s.ytd.netPl >= 0 ? .green : .red,
                                   bold: true)
                        Divider().padding(.leading, 16)
                        FinanceRow(label: "Trades Closed", value: "\(s.ytd.tradeCount)")
                        Divider().padding(.leading, 16)
                        FinanceRow(label: "Winners / Losers",
                                   value: "\(s.ytd.winningTrades) / \(s.ytd.losingTrades)")
                    }
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                    // ── Tax estimate card ──
                    VStack(alignment: .leading, spacing: 0) {
                        SectionHeader(title: "ESTIMATED TAX (\(Int(selectedRate * 100))% BRACKET)",
                                      icon: "dollarsign.circle.fill", color: .orange)
                        Divider().padding(.horizontal)

                        FinanceRow(label: "Taxable Gain",
                                   value: formatDollar(s.tax.taxableGain),
                                   valueColor: .primary)
                        Divider().padding(.leading, 16)
                        FinanceRow(label: "Estimated Tax",
                                   value: "−\(formatDollar(s.tax.estimatedTax))",
                                   valueColor: .red,
                                   bold: true)
                        Divider().padding(.leading, 16)
                        FinanceRow(label: "After-Tax Gain",
                                   value: formatDollar(s.tax.afterTaxGain),
                                   valueColor: s.tax.afterTaxGain >= 0 ? .green : .red,
                                   bold: true)

                        Text(s.tax.disclaimer)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 10)
                    }
                    .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                    // ── Info note ──
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "info.circle.fill")
                            .foregroundStyle(.blue)
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Why all short-term?")
                                .font(.caption).fontWeight(.semibold)
                            Text("The bot holds positions for hours to days — never more than a year. Short-term gains are taxed as ordinary income at your marginal rate.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding()
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color.blue.opacity(0.08)))
                }
            }
            .padding()
        }
        .navigationTitle("Estimated Taxes")
        .navigationBarTitleDisplayMode(.large)
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        error = nil
        do {
            summary = try await APIService.shared.getTaxSummary(bracketRate: selectedRate)
        } catch {
            self.error = "Could not load tax data: \(error.localizedDescription)"
        }
        isLoading = false
    }

    private func formatDollar(_ value: Double) -> String {
        let prefix = value >= 0 ? "+$" : "-$"
        return "\(prefix)\(String(format: "%.2f", abs(value)))"
    }
}

// MARK: - Shared subviews

private struct SectionHeader: View {
    let title: String
    let icon: String
    let color: Color

    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                Circle().fill(color.opacity(0.12)).frame(width: 36, height: 36)
                Image(systemName: icon).foregroundStyle(color).font(.system(size: 16))
            }
            Text(title)
                .font(.caption).fontWeight(.semibold)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding()
    }
}

private struct FinanceRow: View {
    let label: String
    let value: String
    var valueColor: Color = .primary
    var bold: Bool = false

    var body: some View {
        HStack {
            Text(label)
                .font(.subheadline)
                .foregroundStyle(.primary)
            Spacer()
            Text(value)
                .font(.subheadline)
                .fontWeight(bold ? .bold : .regular)
                .foregroundStyle(valueColor)
                .monospacedDigit()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}
