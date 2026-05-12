import SwiftUI

struct AccountCardView: View {
    let account: AccountInfo

    var body: some View {
        VStack(spacing: 8) {
            Text("Portfolio Value")
                .font(.subheadline)
                .foregroundStyle(.secondary)

            Text(String(format: "$%.2f", account.portfolioValue))
                .font(.system(size: 36, weight: .bold, design: .rounded))

            HStack(spacing: 4) {
                Image(systemName: account.dayPl >= 0 ? "arrow.up.right" : "arrow.down.right")
                Text(String(format: "%@$%.2f (%.2f%%) today", account.dayPl >= 0 ? "+" : "", account.dayPl, account.dayPlPercent))
            }
            .font(.subheadline)
            .foregroundStyle(account.dayPl >= 0 ? .green : .red)

            Divider()
                .padding(.vertical, 4)

            HStack {
                VStack(alignment: .leading) {
                    Text("Cash")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(String(format: "$%.2f", account.cash))
                        .font(.subheadline)
                        .fontWeight(.medium)
                }
                Spacer()
                VStack(alignment: .trailing) {
                    Text("Buying Power")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(String(format: "$%.2f", account.buyingPower))
                        .font(.subheadline)
                        .fontWeight(.medium)
                }
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .padding(.horizontal)
    }
}
