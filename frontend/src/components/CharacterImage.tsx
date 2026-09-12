/**
 * A character's picture. The catalogue's characters are stickers (a path in
 * the public folder); anything older is an emoji. Both render through here so
 * no screen has to know which it was given.
 */
export function CharacterImage({
  image,
  className,
  alt = '',
}: {
  image: string
  className?: string
  /** Empty for decoration beside a name that already says who it is. */
  alt?: string
}) {
  if (image.startsWith('/')) return <img src={image} alt={alt} className={className} />
  return (
    <span
      className={className}
      role={alt ? 'img' : undefined}
      aria-label={alt || undefined}
      aria-hidden={alt ? undefined : true}
    >
      {image}
    </span>
  )
}
