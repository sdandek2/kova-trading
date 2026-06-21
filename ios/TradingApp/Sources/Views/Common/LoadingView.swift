import SwiftUI

struct LoadingView: View {
    @State private var isSpinning = false
    @State private var lotusScale: CGFloat = 0.7
    @State private var lotusOpacity: Double = 0

    var body: some View {
        VStack(spacing: 28) {
            // Lotus + spinner combo
            ZStack {
                // Outer glow ring
                Circle()
                    .stroke(LakshmiTheme.purple.opacity(0.08), lineWidth: 28)
                    .frame(width: 100, height: 100)

                // Spinning gradient arc
                Circle()
                    .trim(from: 0, to: 0.72)
                    .stroke(
                        AngularGradient(
                            colors: [LakshmiTheme.pink, LakshmiTheme.purple, LakshmiTheme.blue, LakshmiTheme.pink],
                            center: .center
                        ),
                        style: StrokeStyle(lineWidth: 3, lineCap: .round)
                    )
                    .frame(width: 72, height: 72)
                    .rotationEffect(.degrees(isSpinning ? 360 : 0))
                    .animation(.linear(duration: 1.1).repeatForever(autoreverses: false),
                               value: isSpinning)

                // Lotus emoji centre
                Text("🪷")
                    .font(.system(size: 28))
                    .scaleEffect(lotusScale)
                    .opacity(lotusOpacity)
            }

            // Shimmer skeleton cards
            VStack(spacing: 10) {
                SkeletonCard(width: 260, height: 14)
                SkeletonCard(width: 200, height: 12)
                SkeletonCard(width: 220, height: 12)
            }

            Text("Loading Lakshmi…")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
                .opacity(lotusOpacity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            isSpinning = true
            withAnimation(.spring(response: 0.6, dampingFraction: 0.7)) {
                lotusScale   = 1.0
                lotusOpacity = 1.0
            }
        }
    }
}

private struct SkeletonCard: View {
    let width: CGFloat
    let height: CGFloat
    var body: some View {
        RoundedRectangle(cornerRadius: height / 2)
            .fill(Color(.tertiarySystemBackground))
            .frame(width: width, height: height)
            .shimmer()
    }
}
