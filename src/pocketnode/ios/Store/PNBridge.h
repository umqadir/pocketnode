// The store UI's way into Python: pocketnode_ios.<function>(optional str) -> str,
// called on one serial background queue, answered on the main queue.

#import <UIKit/UIKit.h>

NS_ASSUME_NONNULL_BEGIN

// Exported by AppDelegate.m in store builds.
FOUNDATION_EXTERN NSString *_Nullable pn_store_py_call(const char *func, const char *_Nullable arg);
FOUNDATION_EXTERN BOOL pn_store_python_ready(void);

typedef void (^PNResult)(NSString *_Nullable result);

@interface PNBridge : NSObject
// result is nil when Python isn't running yet, the function doesn't exist, or it raised.
+ (void)call:(NSString *)func arg:(nullable NSString *)arg done:(nullable PNResult)done;
// Parses JSON, returning nil unless the result is of the given class.
+ (nullable id)json:(nullable NSString *)text ofClass:(Class)cls;
@end

// The latest status_json(), polled about once a second while the app is in front.
FOUNDATION_EXTERN NSNotificationName const PNStatusDidChange;

@interface PNStatus : NSObject
@property (class, readonly) PNStatus *shared;
@property (readonly, nullable) NSDictionary *latest;
- (void)start;
- (void)refreshNow;
@end

// A run typed into the console on this iPhone. It goes on when the console is closed.
FOUNDATION_EXTERN NSNotificationName const PNLocalRunDidChange;

@interface PNLocalRun : NSObject
@property (class, readonly) PNLocalRun *shared;
@property (readonly) BOOL running;
@property (readonly, copy, nullable) NSString *output;
@property (readonly, copy, nullable) NSString *error;
@property (readonly) double seconds;
@property (readonly) BOOL finished;  // a run has ended and its result is shown
- (void)run:(NSString *)code;
- (void)stop;
@end

// Helpers shared by the screens.
NSString *PNPlural(NSInteger n, NSString *one, NSString *many);
NSString *PNBytes(id _Nullable value);
NSString *PNString(id _Nullable value);
NSArray *PNArray(id _Nullable value);
NSDictionary *PNDict(id _Nullable value);

NS_ASSUME_NONNULL_END
