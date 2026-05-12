import SwiftUI

struct OrderRowView: View {
    let order: Order

    private var sideColor: Color { order.side == "buy" ? .green : .red }
    private var sideLabel: String { order.side.uppercased() }

    var body: some View {
        HStack(spacing: 12) {
            Text(sideLabel)
                .font(.caption)
                .fontWeight(.bold)
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(sideColor)
                .clipShape(Capsule())

            VStack(alignment: .leading, spacing: 2) {
                Text(order.symbol)
                    .font(.headline)
                Text("\(Int(order.qty)) shares")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 2) {
                if let price = order.filledAvgPrice {
                    Text(String(format: "$%.2f", price))
                        .font(.subheadline)
                        .fontWeight(.medium)
                }
                Text(order.status.capitalized)
                    .font(.caption)
                    .foregroundStyle(order.status == "filled" ? .green : .secondary)
                Text(order.createdAt, style: .relative)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 4)
        .opacity(order.status == "filled" ? 1.0 : 0.6)
    }
}
