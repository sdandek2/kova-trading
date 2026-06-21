import SwiftUI
import Charts

struct PortfolioChartView: View {
    let points: [PortfolioPoint]
    var onPeriodChange: ((String) -> Void)? = nil
    @State private var selectedPeriod = "1W"
    let periods = ["1D", "1W", "1M", "3M"]
    @Environment(\.colorScheme) private var colorScheme

    private var isPositive: Bool {
        (points.last?.equity ?? 0) >= (points.first?.equity ?? 0)
    }

    private var lineColor: Color { isPositive ? LakshmiTheme.positive : LakshmiTheme.negative }

    private var areaGradient: LinearGradient {
        LinearGradient(
            colors: [lineColor.opacity(colorScheme == .dark ? 0.35 : 0.25), .clear],
            startPoint: .top,
            endPoint: .bottom
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            // ── Header ────────────────────────────────────────────────────
            HStack {
                Text("Equity Curve")
                    .font(.headline)
                Spacer()
                if !points.isEmpty {
                    deltaBadge
                }
            }

            // ── Chart ─────────────────────────────────────────────────────
            if points.isEmpty {
                Text("No history available yet")
                    .foregroundStyle(.secondary)
                    .font(.subheadline)
                    .frame(maxWidth: .infinity)
                    .frame(height: 150)
            } else {
                Chart(points) { point in
                    LineMark(
                        x: .value("Date", point.date),
                        y: .value("Equity", point.equity)
                    )
                    .foregroundStyle(lineColor)
                    .lineStyle(StrokeStyle(lineWidth: 2.5))
                    .interpolationMethod(.catmullRom)

                    AreaMark(
                        x: .value("Date", point.date),
                        y: .value("Equity", point.equity)
                    )
                    .foregroundStyle(areaGradient)
                    .interpolationMethod(.catmullRom)
                }
                .frame(height: 160)
                .chartXAxis(.hidden)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [4]))
                            .foregroundStyle(Color(.separator).opacity(0.5))
                        AxisValueLabel {
                            if let v = value.as(Double.self) {
                                Text("$\(Int(v / 1000))k")
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }

            // ── Period picker ─────────────────────────────────────────────
            HStack(spacing: 6) {
                ForEach(periods, id: \.self) { period in
                    Button {
                        selectedPeriod = period
                        onPeriodChange?(period)
                    } label: {
                        Text(period)
                            .font(.caption.weight(.semibold))
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 7)
                            .foregroundStyle(selectedPeriod == period ? .white : .secondary)
                            .background {
                                if selectedPeriod == period {
                                    LakshmiTheme.blueGradient
                                } else {
                                    Color.clear
                                }
                            }
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                }
            }
            .padding(4)
            .background(Color(.tertiarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
        .kovaCard()
        .padding(.horizontal, LakshmiTheme.pagePad)
    }

    @ViewBuilder
    private var deltaBadge: some View {
        if let first = points.first, let last = points.last {
            let delta = last.equity - first.equity
            let pct = first.equity > 0 ? delta / first.equity * 100 : 0
            let positive = delta >= 0
            HStack(spacing: 3) {
                Image(systemName: positive ? "arrow.up.right" : "arrow.down.right")
                    .font(.caption2.weight(.bold))
                Text(String(format: "%@%.2f%%", positive ? "+" : "", pct))
                    .font(.caption.weight(.semibold))
            }
            .foregroundStyle(positive ? LakshmiTheme.positive : LakshmiTheme.negative)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background((positive ? LakshmiTheme.positive : LakshmiTheme.negative).opacity(0.12))
            .clipShape(Capsule())
        }
    }
}
