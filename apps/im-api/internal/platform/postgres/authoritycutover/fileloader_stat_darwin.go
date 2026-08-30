//go:build darwin

package authoritycutover

import "golang.org/x/sys/unix"

func trustedFileFingerprintForDescriptor(descriptor int) (trustedFileFingerprint, error) {
	var value unix.Stat_t
	if err := unix.Fstat(descriptor, &value); err != nil {
		return trustedFileFingerprint{}, err
	}
	return trustedFileFingerprint{
		device:          uint64(value.Dev),
		inode:           value.Ino,
		mode:            uint32(value.Mode),
		linkCount:       uint64(value.Nlink),
		ownerUID:        value.Uid,
		ownerGID:        value.Gid,
		size:            value.Size,
		modifiedSeconds: value.Mtim.Sec,
		modifiedNanos:   value.Mtim.Nsec,
		changedSeconds:  value.Ctim.Sec,
		changedNanos:    value.Ctim.Nsec,
	}, nil
}
