package authoritycutover

import (
	"errors"
	"io/fs"
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

func validTrustedFileIdentity(identity TrustedFileIdentity) bool {
	if identity.Mode != identity.Mode.Perm() {
		return false
	}
	return identity.Mode == 0o400 || identity.Mode == 0o440
}
