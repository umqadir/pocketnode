// The App Store edition's interface: one navigation stack of inset grouped tables,
// rendered from storehost.status_json() (see PNBridge.h).

#import <UIKit/UIKit.h>

NS_ASSUME_NONNULL_BEGIN

// The window's root: a navigation controller showing the main screen.
UIViewController *PNMakeStoreRootViewController(void);

// What the host app does for the UI (AppDelegate implements it).
@protocol PNStoreHost <NSObject>
- (void)keepComputing;  // ask iOS to keep computing in the background; only ever from a tap
@end

typedef NS_ENUM(NSInteger, PNRowKind) {
    PNRowPlain,       // text only
    PNRowValue,       // text and a value on the right
    PNRowSubtitle,    // text and a second line
    PNRowToggle,      // text and a switch
    PNRowButton,      // tinted text that does something
    PNRowDestructive, // red text that does something
    PNRowNote,        // secondary text, nothing to do (empty states)
};

@interface PNRow : NSObject
@property (copy) NSString *ident;  // stable across refreshes
@property (assign) PNRowKind kind;
@property (copy) NSString *text;
@property (copy, nullable) NSString *detail;
@property (assign) BOOL on;
@property (assign) BOOL enabled;
@property (assign) BOOL disclosure;
@property (assign) BOOL monospaced;
@property (copy, nullable) NSString *hint;  // VoiceOver hint
@property (copy, nullable) void (^action)(void);
@property (copy, nullable) void (^toggled)(BOOL on, UISwitch *sender);
@property (copy, nullable) NSString *deleteTitle;
@property (copy, nullable) void (^deleted)(void);
+ (instancetype)ident:(NSString *)ident kind:(PNRowKind)kind text:(NSString *)text detail:(nullable NSString *)detail;
@end

@interface PNSection : NSObject
@property (copy, nullable) NSString *header;
@property (copy, nullable) NSString *footer;
@property (strong) NSArray<PNRow *> *rows;
+ (instancetype)header:(nullable NSString *)header rows:(NSArray<PNRow *> *)rows footer:(nullable NSString *)footer;
@end

// An inset grouped table showing sections of rows. Setting `sections` again with the
// same rows updates the visible cells in place, so switches and swipes aren't disturbed.
@interface PNTableViewController : UITableViewController
@property (strong, nonatomic) NSArray<PNSection *> *sections;
- (void)statusChanged;  // subclasses rebuild from PNStatus.shared.latest
@end

@interface PNMainViewController : PNTableViewController
@end

// A read-only text screen: source code (monospaced) or plain text. With `decide`, it shows
// Allow and Don't Allow in a toolbar.
@interface PNTextViewController : UIViewController
- (instancetype)initWithTitle:(NSString *)title text:(nullable NSString *)text monospaced:(BOOL)mono;
// Loads the text from pocketnode_ios.<func>(arg).
- (instancetype)initWithTitle:(NSString *)title func:(NSString *)func arg:(NSString *)arg;
@property (copy, nullable) NSString *heading;  // a small line above the text
@property (copy, nullable) NSString *emptyText;
@property (copy, nullable) void (^decide)(BOOL allow);
@end

@interface PNPairingViewController : UIViewController
@end

@interface PNMacViewController : PNTableViewController
- (instancetype)initWithPairId:(NSString *)pairId;
@end

@interface PNCodeListViewController : PNTableViewController
@end

@interface PNFetchedViewController : PNTableViewController
@end

@interface PNPackagesViewController : PNTableViewController
@end

@interface PNConsoleViewController : UIViewController
@end

// Shared by the screens.
NSString *_Nullable PNDateText(id _Nullable epoch);
NSString *PNDuration(double seconds);
UIFont *PNMonoFont(UIFontTextStyle style);
void PNConfirmForget(UIViewController *vc, NSDictionary *mac, void (^_Nullable done)(void));
void PNApprove(NSString *code, BOOL allow, void (^_Nullable done)(void));

NS_ASSUME_NONNULL_END
