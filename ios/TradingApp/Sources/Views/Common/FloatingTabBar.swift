import SwiftUI

struct FloatingTabBar: View {
    @Binding var selected: Int
    var namespace: Namespace.ID

    private let items: [(icon: String, filled: String, label: String)] = [
        ("house",                "house.fill",               "Home"),
        ("list.bullet.rectangle","list.bullet.rectangle.fill","Orders"),
        ("flask",                "flask.fill",               "Labs"),
        ("brain.head.profile",   "brain.head.profile",       "Pure AI"),
        ("arrow.2.circlepath",   "arrow.2.circlepath",       "Wheel"),
        ("ellipsis.circle",      "ellipsis.circle.fill",     "More")
    ]

    var body: some View {
        HStack(spacing: 2) {
            ForEach(items.indices, id: \.self) { i in
                let sel = i == selected
                Button {
                    withAnimation(LakshmiTheme.springSnappy) { selected = i }
                } label: {
                    HStack(spacing: sel ? 5 : 0) {
                        Image(systemName: sel ? items[i].filled : items[i].icon)
                            .font(.system(size: 15, weight: .semibold))
                        if sel {
                            Text(items[i].label)
                                .font(.system(size: 11, weight: .bold))
                                .lineLimit(1)
                                .transition(
                                    .asymmetric(
                                        insertion: .opacity.combined(with: .scale(scale: 0.8, anchor: .leading)),
                                        removal: .opacity
                                    )
                                )
                        }
                    }
                    .foregroundStyle(sel ? .white : Color(.systemGray))
                    .padding(.horizontal, sel ? 13 : 10)
                    .padding(.vertical, 9)
                    .frame(maxWidth: sel ? .infinity : nil)
                    .background {
                        if sel {
                            Capsule()
                                .fill(LakshmiTheme.brandGradient)
                                .matchedGeometryEffect(id: "tabSelPill", in: namespace)
                                .shadow(color: LakshmiTheme.gold.opacity(0.4), radius: 8, x: 0, y: 3)
                        }
                    }
                }
                .buttonStyle(PressScaleButtonStyle(scale: 0.91))
                .frame(maxWidth: sel ? .infinity : nil)
            }
        }
        .padding(5)
        .background(.ultraThinMaterial, in: Capsule())
        .overlay(Capsule().stroke(.white.opacity(0.10), lineWidth: 1))
        .shadow(color: .black.opacity(0.13), radius: 18, x: 0, y: 6)
        .padding(.horizontal, 18)
        .padding(.bottom, 28)
    }
}
