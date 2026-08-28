package authoritycutover

import (
	"errors"
	"io/fs"
	"time"
)

var (
	ErrInvalidFileIdentity     = errors.New("invalid authority cutover input file identity")
	ErrUnsafeInputFile         = errors.New("unsafe authority cutover input file")
	ErrInputFileChanged        = errors.New("authority cutover input file changed during read")
	ErrUnsupportedFilePlatform = errors.New("authority cutover input file loading is unsupported on this platform")
)

// TrustedFileIdentity freezes the deployment-controller ownership and exact read-only mode that
// must be observed through the already-open descriptor. Owner and group IDs are numeric Linux/
// Unix identities; names are deliberately not resolved through ambient NSS during a cutover.
type TrustedFileIdentity struct {
	OwnerUID uint32
	OwnerGID uint32
	Mode     fs.FileMode
}

// LoadPlanFile reads an immutable, single-link regular file through a no-symlink path walk and
// then applies the same strict decoder used for in-memory input. It never follows a parent or final
// symlink and never reports the path or raw bytes in its errors.
func LoadPlanFile(path string, identity TrustedFileIdentity) (Plan, error) {
	if !validTrustedFileIdentity(identity) {
		return Plan{}, ErrInvalidFileIdentity
	}
	raw, err := readTrustedRegularFile(path, identity, maximumPlanBytes)
	if err != nil {
		return Plan{}, err
	}
	return DecodePlan(raw)
}

// VerifyApprovalFile reads an immutable approval through the same descriptor-based trust boundary
// as LoadPlanFile and immediately verifies it. Raw approval bytes and reusable signatures never
// cross the returned API boundary.
func VerifyApprovalFile(
	plan Plan,
	path string,
	identity TrustedFileIdentity,
	verifier ApprovalVerifier,
	now time.Time,
) (VerifiedApproval, error) {
	if !validTrustedFileIdentity(identity) {
		return VerifiedApproval{}, ErrInvalidFileIdentity
	}
	raw, err := readTrustedRegularFile(path, identity, maximumApprovalBytes)
	if err != nil {
		return VerifiedApproval{}, err
	}
	return verifier.Verify(plan, raw, now)
}

func validTrustedFileIdentity(identity TrustedFileIdentity) bool {
	if identity.Mode != identity.Mode.Perm() {
		return false
	}
	return identity.Mode == 0o400 || identity.Mode == 0o440
}
