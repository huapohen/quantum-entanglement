//go:build !darwin && !linux

package authoritycutover

func readTrustedRegularFile(
	_ string,
	_ TrustedFileIdentity,
	_ int,
) ([]byte, error) {
	return nil, ErrUnsupportedFilePlatform
}
