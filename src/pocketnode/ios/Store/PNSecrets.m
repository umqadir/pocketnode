// The phone's pairing secrets, kept in the keychain for pocketnode.pairing.KeychainBackend
// (which finds these functions with ctypes.CDLL(None)). One generic password per key:
// service = the bundle identifier, account = the key, readable after the first unlock,
// on this device only, never synchronized.

#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <string.h>

static NSMutableDictionary *pn_secret_query(const char *key) {
    NSString *service = [NSBundle mainBundle].bundleIdentifier ?: @"pocketnode";
    NSString *account = [NSString stringWithUTF8String:key] ?: @"";
    return [@{
        (__bridge id)kSecClass: (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService: service,
        (__bridge id)kSecAttrAccount: account,
        (__bridge id)kSecAttrSynchronizable: @NO,
    } mutableCopy];
}

// 0 = saved; otherwise the keychain's error code.
__attribute__((visibility("default"), used))
int pocketnode_secret_set(const char *key, const char *value) {
    if (!key || !value) return -1;
    @autoreleasepool {
        NSData *data = [NSData dataWithBytes:value length:strlen(value)];
        NSMutableDictionary *query = pn_secret_query(key);
        NSDictionary *update = @{
            (__bridge id)kSecValueData: data,
            (__bridge id)kSecAttrAccessible: (__bridge id)kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        };
        OSStatus st = SecItemUpdate((__bridge CFDictionaryRef)query, (__bridge CFDictionaryRef)update);
        if (st == errSecItemNotFound) {
            [query addEntriesFromDictionary:update];
            st = SecItemAdd((__bridge CFDictionaryRef)query, NULL);
        }
        return st == errSecSuccess ? 0 : (int)st;
    }
}

// Bytes written to buf (not counting the terminating NUL, added when it fits),
// -1 when there is no such key, -2 when buf is too small, another negative
// number for any other keychain error.
__attribute__((visibility("default"), used))
int pocketnode_secret_get(const char *key, char *buf, int cap) {
    if (!key || !buf || cap < 0) return -3;
    @autoreleasepool {
        NSMutableDictionary *query = pn_secret_query(key);
        query[(__bridge id)kSecReturnData] = @YES;
        query[(__bridge id)kSecMatchLimit] = (__bridge id)kSecMatchLimitOne;
        CFTypeRef result = NULL;
        OSStatus st = SecItemCopyMatching((__bridge CFDictionaryRef)query, &result);
        if (st == errSecItemNotFound) return -1;
        if (st != errSecSuccess) return st < 0 ? (int)st : -3;
        NSData *data = (__bridge_transfer NSData *)result;
        if (data.length > (NSUInteger)cap) return -2;
        memcpy(buf, data.bytes, data.length);
        if (data.length < (NSUInteger)cap) buf[data.length] = '\0';
        return (int)data.length;
    }
}

// 0 = deleted, or there was nothing to delete.
__attribute__((visibility("default"), used))
int pocketnode_secret_delete(const char *key) {
    if (!key) return -1;
    @autoreleasepool {
        OSStatus st = SecItemDelete((__bridge CFDictionaryRef)pn_secret_query(key));
        return (st == errSecSuccess || st == errSecItemNotFound) ? 0 : (int)st;
    }
}

#if DEBUG
// Present only in Debug builds: storehost allows its automation (a pairing code from the
// environment) only when this exists and the bundle's settings say "debug".
__attribute__((visibility("default"), used))
int pocketnode_debug_build(void) {
    return 1;
}
#endif
