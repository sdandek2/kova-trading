import SwiftUI

struct PositionRowView: View {
    let position: Position

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(position.symbol)
                    .font(.headline)
                Text(String(format: "%d shares · avg $%.2f", Int(position.qty), position.avgEntryPrice))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(String(format: "$%.2f", position.currentPrice))
                    .font(.subheadline)
                    .fontWeight(.medium)
                Text(String(format: "%@$%.2f (%.1f%%)", position.unrealizedPl >= 0 ? "+" : "", position.unrealizedPl, position.unrealizedPlPercent))
                    .font(.caption)
                    .foregroundStyle(position.unrealizedPl >= 0 ? .green : .red)
            }
        }
        .padding(.vertical, 4)
    }
}
