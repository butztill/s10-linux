// SPDX-License-Identifier: GPL-2.0-only
#include "photoprocessing.h"
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QDebug>
#include <cmath>
#include <random>

int main(int argc, char **argv)
{
    QCoreApplication app(argc, argv);
    QImage source(640, 480, QImage::Format_RGB32);
    std::mt19937 random(4242);
    std::normal_distribution<> noise(0, 8);
    double before = 0, after = 0;
    for (int y = 0; y < source.height(); ++y)
        for (int x = 0; x < source.width(); ++x) {
            const int base = x < 320 ? 60 : 195;
            source.setPixel(x, y, qRgb(std::clamp(base + int(noise(random)), 0, 255),
                std::clamp(base + int(noise(random)), 0, 255), std::clamp(base + int(noise(random)), 0, 255)));
        }
    const QImage original = source.copy();
    const QImage filtered = S10Photo::denoise(source);
    for (int y = 1; y < source.height() - 1; ++y)
        for (int x = 1; x < source.width() - 1; ++x) {
            const int base = x < 320 ? 60 : 195;
            for (int (*channel)(QRgb) : {static_cast<int (*)(QRgb)>(qRed),
                 static_cast<int (*)(QRgb)>(qGreen), static_cast<int (*)(QRgb)>(qBlue)}) {
                before += std::pow(channel(source.pixel(x, y)) - base, 2);
                after += std::pow(channel(filtered.pixel(x, y)) - base, 2);
            }
        }
    qInfo() << "Noise MSE ratio" << after / before;
    if (filtered.size() != source.size() || source != original || after >= before * 0.80)
        return 1;
    // Flat saturated colours and high-contrast edges must survive unchanged.
    QImage edges(64, 64, QImage::Format_RGB32);
    for (int y = 0; y < 64; ++y)
        for (int x = 0; x < 64; ++x)
            edges.setPixel(x, y, x < 32 ? qRgb(240, 20, 10) : qRgb(10, 20, 240));
    const auto clean = S10Photo::denoise(edges);
    if (std::abs(qRed(clean.pixel(31, 32)) - 240) > 3 ||
        std::abs(qBlue(clean.pixel(32, 32)) - 240) > 3)
        return 2;
    QImage rgbx(80, 80, QImage::Format_RGBX8888);
    rgbx.fill(QColor(210, 75, 25));
    const auto rgb = S10Photo::denoise(rgbx);
    if (rgb.pixelColor(20, 20) != QColor(210, 75, 25))
        return 10;
    // The GUI copy must own its pixels, even when no resize is necessary.
    std::vector<uchar> pixels(64 * 48 * 4, 100);
    QImage borrowed(pixels.data(), 64, 48, QImage::Format_RGBX8888);
    const auto owned = S10Photo::preview(borrowed);
    std::fill(pixels.begin(), pixels.end(), 0);
    if (qRed(owned.pixel(10, 10)) != 100)
        return 3;
    QImage large(4028, 3024, QImage::Format_RGB32);
    large.fill(qRgb(100, 120, 140));
    QElapsedTimer timer;
    timer.start();
    const auto small = S10Photo::preview(large);
    qInfo() << "Preview" << small.size() << "bytes" << small.sizeInBytes() << "ms" << timer.elapsed();
    if (small.width() != 1600 || small.height() != 1201)
        return 4;
    timer.restart();
    const auto full = S10Photo::denoise(large);
    qInfo() << "12 MP denoise ms" << timer.elapsed();
    if (full.size() != large.size() || full.pixel(100, 100) != large.pixel(100, 100))
        return 5;
    qInfo() << "PASS noise, edge, colour, ownership and resolution checks";
}
