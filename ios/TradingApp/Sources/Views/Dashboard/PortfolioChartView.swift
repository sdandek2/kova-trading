import SwiftUI
import Charts

struct PortfolioChartView: View {
    let points: [PortfolioPoint]
    var onPeriodChange: ((String) -> Void)? = nil
    @State private var selectedPeriod = "1W"
    let periods = ["1D", "1W", "1M", "3M"]

    private var isPositive: Bool {
        (points.last?.equity ?? 0) >= (points.first?.equity ?? 0)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Equity Curve")
                .font(.headline)

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
                    .foregroundStyle(isPositive ? Color.green : Color.red)

                    AreaMark(
                        x: .value("Date", point.date),
                        y: .value("Equity", point.equity)
                    )
                    .foregroundStyle(
                        LinearGradient(
                            colors: [
                                (isPositive ? Color.green : Color.red).opacity(0.3),
                                .clear
                            ],
                            startPoint: .top,
                            endPoint: .bottom
                        )
                    )
                }
                .frame(height: 150)
                .chartXAxis(.hidden)
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisValueLabel {
                            if let v = value.as(Double.self) {
                                Text("$\(Int(v / 1000))k")
                                    .font(.caption2)
                            }
                        }
                    }
                }
            }

            Picker("Period", selection: $selectedPeriod) {
                ForEach(periods, id: \.self) { p in
                    Text(p).tag(p)
                }
            }
            .pickerStyle(.segmented)
            .onChange(of: selectedPeriod) { _, newPeriod in
                onPeriodChange?(newPeriod)
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
    }
}
