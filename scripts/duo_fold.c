// duo_fold — set the hinge angle of the iPhone Duo Simulator from inside it.
//
//   duo_fold set <degrees>            0 (shut) … 180 (flat)
//   duo_fold sweep <from> <to> [secs]  animate, default 1s at 60Hz
//   duo_fold path <secs> <a> <b> [c…]  animate through each angle in turn
//   duo_fold orient <portrait|pud|landscape-left|landscape-right>
//
// The Simulator exposes no public way to fold a device: the only control is the
// hidden hinge slider in Xcode's Device Hub, which posts one vendor-defined HID
// event. This posts the same event, so `xcrun simctl spawn <udid> duo_fold set 90`
// folds the device without depending on any third-party helper.
//
//   usage page 0xFF61, usage 0x5B, payload = binary-plist of
//   {provider: com.apple.Virtualization, source: hinge-slider-control,
//    type: range, value: <degrees>}
//
// Device Hub's orientation picker posts the same event with
// {source: orientation-picker-control, type: enum, value: <name>}, which turns
// the device as a hand would: `simctl` has no rotate command, and on a headless
// simulator XCUIDevice.orientation changes nothing.
//
// Build once with the iOS Simulator SDK (duo.py does this for you and caches it):
//   xcrun -sdk iphonesimulator clang -arch arm64 \
//     -mios-simulator-version-min=17.0 -O2 -o duo_fold duo_fold.c \
//     -framework IOKit -framework CoreFoundation && codesign -f -s - duo_fold
//
// From https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/

#include <CoreFoundation/CoreFoundation.h>
#include <mach/mach_time.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct __IOHIDEvent *IOHIDEventRef;
typedef struct __IOHIDEventSystemClient *IOHIDEventSystemClientRef;

extern IOHIDEventRef IOHIDEventCreateVendorDefinedEvent(
    CFAllocatorRef allocator, uint64_t timestamp, uint32_t usagePage,
    uint32_t usage, uint32_t version, const uint8_t *data, CFIndex length,
    uint32_t options);
extern IOHIDEventSystemClientRef IOHIDEventSystemClientCreateWithType(
    CFAllocatorRef allocator, int type, CFDictionaryRef properties);
extern void IOHIDEventSystemClientDispatchEvent(IOHIDEventSystemClientRef client,
                                                IOHIDEventRef event);
extern CFDataRef IOCFSerialize(CFTypeRef object, CFOptionFlags options);

enum {
    kHingeUsagePage = 0xFF61,
    kHingeUsage = 0x5B,
    kClientTypeSimple = 4,
    kSerializeBinary = 1,
    kSweepHz = 60,
    kSettleMicros = 120000,  // let the system deliver the event before exiting
};

static double clamp_angle(double degrees) {
    if (degrees < 0) return 0;
    if (degrees > 180) return 180;
    return degrees;
}

static void post_control(IOHIDEventSystemClientRef client, CFStringRef source,
                         CFStringRef type, CFTypeRef value) {
    const void *keys[] = {CFSTR("provider"), CFSTR("source"), CFSTR("type"), CFSTR("value")};
    const void *values[] = {CFSTR("com.apple.Virtualization"), source, type, value};
    CFDictionaryRef payload = CFDictionaryCreate(NULL, keys, values, 4,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDataRef data = payload ? IOCFSerialize(payload, kSerializeBinary) : NULL;
    if (data) {
        IOHIDEventRef event = IOHIDEventCreateVendorDefinedEvent(NULL, mach_absolute_time(),
            kHingeUsagePage, kHingeUsage, 0, CFDataGetBytePtr(data), CFDataGetLength(data), 0);
        if (event) {
            IOHIDEventSystemClientDispatchEvent(client, event);
            CFRelease(event);
        }
        CFRelease(data);
    }
    if (payload) CFRelease(payload);
}

static void post_angle(IOHIDEventSystemClientRef client, double degrees) {
    CFNumberRef value = CFNumberCreate(NULL, kCFNumberDoubleType, &degrees);
    post_control(client, CFSTR("hinge-slider-control"), CFSTR("range"), value);
    CFRelease(value);
}

static int post_orientation(IOHIDEventSystemClientRef client, const char *name) {
    const char *known[] = {"portrait", "pud", "landscape-left", "landscape-right"};
    for (size_t i = 0; i < sizeof(known) / sizeof(known[0]); i++) {
        if (strcmp(name, known[i]) == 0) {
            CFStringRef value = CFStringCreateWithCString(NULL, name, kCFStringEncodingUTF8);
            post_control(client, CFSTR("orientation-picker-control"), CFSTR("enum"), value);
            CFRelease(value);
            return 0;
        }
    }
    fprintf(stderr, "duo_fold: orientation must be portrait, pud, landscape-left or landscape-right\n");
    return 64;
}

static int usage(void) {
    fprintf(stderr, "usage: duo_fold set <degrees>\n"
                    "       duo_fold sweep <from> <to> [seconds]\n"
                    "       duo_fold path <seconds> <a> <b> [c ...]\n"
                    "       duo_fold orient <portrait|pud|landscape-left|landscape-right>\n");
    return 64;
}

int main(int argc, char **argv) {
    if (argc < 3) return usage();

    IOHIDEventSystemClientRef client =
        IOHIDEventSystemClientCreateWithType(NULL, kClientTypeSimple, NULL);
    if (!client) {
        fprintf(stderr, "duo_fold: no HID event system client (run inside the Simulator)\n");
        return 1;
    }

    int status = 0;
    if (strcmp(argv[1], "set") == 0 && argc == 3) {
        post_angle(client, clamp_angle(atof(argv[2])));
    } else if (strcmp(argv[1], "sweep") == 0 && (argc == 4 || argc == 5)) {
        double from = clamp_angle(atof(argv[2]));
        double to = clamp_angle(atof(argv[3]));
        double seconds = fmax(0, argc == 5 ? atof(argv[4]) : 1.0);
        int steps = (int)fmax(1, round(seconds * kSweepHz));
        for (int i = 0; i <= steps; i++) {
            post_angle(client, from + (to - from) * i / steps);
            if (i < steps) usleep((useconds_t)(seconds * 1e6 / steps));
        }
    } else if (strcmp(argv[1], "path") == 0 && argc >= 5) {
        // One motion through several angles (a bend and back, say), so a
        // gesture timed in seconds isn't stretched by a process per segment.
        double seconds = fmax(0, atof(argv[2]));
        int points = argc - 3;
        int stepsPerLeg = (int)fmax(1, round(seconds * kSweepHz / (points - 1)));
        for (int leg = 0; leg < points - 1; leg++) {
            double from = clamp_angle(atof(argv[3 + leg]));
            double to = clamp_angle(atof(argv[4 + leg]));
            for (int i = (leg == 0 ? 0 : 1); i <= stepsPerLeg; i++) {
                post_angle(client, from + (to - from) * i / stepsPerLeg);
                usleep((useconds_t)(seconds * 1e6 / ((points - 1) * stepsPerLeg)));
            }
        }
    } else if (strcmp(argv[1], "orient") == 0 && argc == 3) {
        status = post_orientation(client, argv[2]);
    } else {
        status = usage();
    }

    if (status == 0) usleep(kSettleMicros);
    CFRelease(client);
    return status;
}
