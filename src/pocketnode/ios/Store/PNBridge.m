#import "PNBridge.h"

NSNotificationName const PNStatusDidChange = @"PNStatusDidChange";
NSNotificationName const PNLocalRunDidChange = @"PNLocalRunDidChange";

#pragma mark - Helpers

NSString *PNPlural(NSInteger n, NSString *one, NSString *many) {
    return [NSString stringWithFormat:@"%ld %@", (long)n, n == 1 ? one : many];
}

NSString *PNBytes(id value) {
    if (![value isKindOfClass:[NSNumber class]]) return @"Unknown";
    static NSByteCountFormatter *f;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        f = [NSByteCountFormatter new];
        f.countStyle = NSByteCountFormatterCountStyleMemory;
        f.allowsNonnumericFormatting = NO;  // "0 bytes", not "Zero KB"
    });
    return [f stringFromByteCount:[value longLongValue]];
}

NSString *PNString(id value) {
    if ([value isKindOfClass:[NSString class]]) return value;
    if ([value isKindOfClass:[NSNumber class]]) return [value stringValue];
    return @"";
}

NSArray *PNArray(id value) {
    return [value isKindOfClass:[NSArray class]] ? value : @[];
}

NSDictionary *PNDict(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

#pragma mark - Bridge

@implementation PNBridge

+ (dispatch_queue_t)queue {
    static dispatch_queue_t q;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        q = dispatch_queue_create("pocketnode.ui.python", dispatch_queue_attr_make_with_qos_class(
            DISPATCH_QUEUE_SERIAL, QOS_CLASS_USER_INITIATED, 0));
    });
    return q;
}

+ (void)call:(NSString *)func arg:(NSString *)arg done:(PNResult)done {
    NSString *f = [func copy], *a = [arg copy];
    dispatch_async([self queue], ^{
        NSString *out = pn_store_python_ready() ? pn_store_py_call(f.UTF8String, a.UTF8String) : nil;
        if (done) dispatch_async(dispatch_get_main_queue(), ^{ done(out); });
    });
}

+ (id)json:(NSString *)text ofClass:(Class)cls {
    NSData *d = [text dataUsingEncoding:NSUTF8StringEncoding];
    if (!d) return nil;
    id obj = [NSJSONSerialization JSONObjectWithData:d options:0 error:nil];
    return [obj isKindOfClass:cls] ? obj : nil;
}

@end

#pragma mark - Status

@interface PNStatus ()
@property (strong) NSTimer *timer;
@property (assign) BOOL inFlight;
@property (assign) BOOL again;
@property (readwrite, nullable) NSDictionary *latest;
@end

@implementation PNStatus

+ (PNStatus *)shared {
    static PNStatus *s;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ s = [PNStatus new]; });
    return s;
}

- (void)start {
    NSNotificationCenter *nc = [NSNotificationCenter defaultCenter];
    [nc addObserver:self selector:@selector(pause) name:UISceneDidEnterBackgroundNotification object:nil];
    [nc addObserver:self selector:@selector(resume) name:UISceneWillEnterForegroundNotification object:nil];
    [self resume];
}

- (void)resume {
    if (self.timer) return;
    __weak PNStatus *weak = self;
    self.timer = [NSTimer scheduledTimerWithTimeInterval:1.0 repeats:YES block:^(NSTimer *t) { [weak poll]; }];
    self.timer.tolerance = 0.1;
    [self refreshNow];
}

- (void)pause {
    [self.timer invalidate];
    self.timer = nil;
}

- (void)poll {
    if (!self.inFlight) [self refreshNow];  // the regular tick skips while a poll is out
}

- (void)refreshNow {
    // A poll already on its way may have been read before an action just taken: poll again after it.
    if (self.inFlight) { self.again = YES; return; }
    self.inFlight = YES;
    [PNBridge call:@"status_json" arg:nil done:^(NSString *result) {
        self.inFlight = NO;
        if (self.again) {  // this answer may predate the action: use the next one instead
            self.again = NO;
            [self refreshNow];
            return;
        }
        NSDictionary *st = [PNBridge json:result ofClass:[NSDictionary class]];
        if (!st) return;
        self.latest = st;
        // The screen stays on only while asked to and a Mac is connected.
        BOOL awake = [st[@"keep_awake"] boolValue] && PNArray(st[@"sessions"]).count > 0;
        if ([UIApplication sharedApplication].idleTimerDisabled != awake)
            [UIApplication sharedApplication].idleTimerDisabled = awake;
        [[NSNotificationCenter defaultCenter] postNotificationName:PNStatusDidChange object:self];
    }];
}

@end

#pragma mark - Local runs

@interface PNLocalRun ()
@property (copy) NSString *jobId;
@property (readwrite) BOOL running;
@property (readwrite) BOOL finished;
@property (readwrite, copy) NSString *output;
@property (readwrite, copy) NSString *error;
@property (readwrite) double seconds;
@property (strong) NSTimer *timer;
@end

@implementation PNLocalRun

+ (PNLocalRun *)shared {
    static PNLocalRun *s;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ s = [PNLocalRun new]; });
    return s;
}

- (void)changed {
    [[NSNotificationCenter defaultCenter] postNotificationName:PNLocalRunDidChange object:self];
}

- (void)run:(NSString *)code {
    if (self.running) return;
    self.running = YES;
    self.finished = NO;
    self.output = @"";
    self.error = nil;
    self.seconds = 0;
    self.jobId = nil;
    [self changed];
    [PNBridge call:@"run_local" arg:code done:^(NSString *job) {
        if (job.length == 0) {
            self.running = NO;
            self.finished = YES;
            self.error = @"Python isn't ready yet. Try again in a moment.";
            [self changed];
            return;
        }
        self.jobId = job;
        __weak PNLocalRun *weak = self;
        self.timer = [NSTimer scheduledTimerWithTimeInterval:0.3 repeats:YES block:^(NSTimer *t) { [weak poll]; }];
    }];
}

- (void)poll {
    NSString *job = self.jobId;
    if (!job) return;
    [PNBridge call:@"local_poll" arg:job done:^(NSString *result) {
        if (![job isEqualToString:self.jobId] || !self.running) return;
        NSDictionary *r = [PNBridge json:result ofClass:[NSDictionary class]];
        if (!r) return;
        self.output = PNString(r[@"out"]);
        self.error = [r[@"error"] isKindOfClass:[NSString class]] ? r[@"error"] : nil;
        self.seconds = [r[@"seconds"] doubleValue];
        if ([r[@"done"] boolValue]) {
            [self.timer invalidate];
            self.timer = nil;
            self.running = NO;
            self.finished = YES;
        }
        [self changed];
    }];
}

- (void)stop {
    if (self.jobId) [PNBridge call:@"local_cancel" arg:self.jobId done:nil];
}

@end
