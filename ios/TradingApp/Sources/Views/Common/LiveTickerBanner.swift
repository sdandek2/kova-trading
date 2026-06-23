import SwiftUI

// Continuously scrolling stock ticker — shows open positions with live price + P&L%.
// Loops seamlessly by duplicating the item array and animating offset.

struct LiveTickerBanner: View {
    let positions: [Position]
    @State private var offset: CGFloat = 0

    private let cellW: CGFloat   = 138
    private let cellGap: CGFloat = 10

    private func loopW(_ count: Int) -> CGFloat {
        CGFloat(count) * (cellW + cellGap)
    }

    var body: some View {
        GeometryReader { geo in
            if positions.isEmpty {
                HStack {
                    Circle().fill(LakshmiTheme.positive).frame(width: 6, height: 6)
                    Text("MARKET LIVE — no open positions")
                        .font(.system(size: 10, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
                .frame(width: geo.size.width, alignment: .center)
            } else {
                let lw = loopW(positions.count)
                HStack(spacing: cellGap) {
                    ForEach(0..<2, id: \.self) { _ in
                        ForEach(positions) { pos in
                            TickerCell(position: pos).frame(width: cellW)
                        }
                    }
                }
                .offset(x: offset)
                .onAppear {
                    offset = 0
                    withAnimation(
                        .linear(duration: max(Double(positions.count) * 4.0, 10.0))
                            .repeatForever(autoreverses: false)
                    ) {
                        offset = -lw
                    }
                }
                .onChange(of: positions.count) { _ in
                    offset = 0
                    withAnimation(
                        .linear(duration: max(Double(positions.count) * 4.0, 10.0))
                            .repeatForever(autoreverses: false)
                    ) {
                        offset = -loopW(positions.count)
                    }
                }
            }
        }
        .frame(height: 34)
        .clipped()
        .overlay(alignment: .leading) {
            LinearGradient(
                colors: [LakshmiTheme.pageBackground, .clear],
                startPoint: .leading, endPoint: .trailing
            )
            .frame(width: 20)
            .allowsHitTesting(false)
        }
        .overlay(alignment: .trailing) {
            LinearGradient(
                colors: [.clear, LakshmiTheme.pageBackground],
                startPoint: .leading, endPoint: .trailing
            )
            .frame(width: 20)
            .allowsHitTesting(false)
        }
    }
}

private struct TickerCell: View {
    let position: Position

    private var up: Bool   { position.unrealizedPlPercent >= 0 }
    private var col: Color { up ? LakshmiTheme.positive : LakshmiTheme.negative }
    private var sign: String { up ? "+" : "" }

    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(col).frame(width: 5, height: 5)

            Text(position.symbol)
                .font(.system(size: 11, weight: .bold, design: .monospaced))
                .foregroundStyle(.primary)

            Spacer(minLength: 0)

            VStack(alignment: .trailing, spacing: 0) {
                Text(String(format: "$%.2f", position.currentPrice))
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(.primary)
                Text(String(format: "%@%.1f%%", sign, position.unrealizedPlPercent))
                    .font(.system(size: 9, weight: .bold, design: .monospaced))
                    .foregroundStyle(col)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(col.opacity(0.06))
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .stroke(col.opacity(0.18), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 7))
    }
}
